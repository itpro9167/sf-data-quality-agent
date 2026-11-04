"""The apply step: the ONLY code path that writes to Salesforce.

Safety properties, in order of evaluation for each proposal:

1. Only proposals whose status is ``approved`` are even loaded.
2. The write policy is re-checked (defense in depth; the DB could have been edited by hand).
3. *Optimistic concurrency*: every record is re-read and each ``before`` value must still match.
   If someone edited the record since the proposal was made, we skip it (``conflict``) rather
   than overwrite their work.
4. ``--dry-run`` stops here: it records the exact operations it *would* run, and writes nothing
   to Salesforce.
5. Every outcome -- dry run, success, conflict, failure -- is appended to the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from sfdq.models import ActionType, ApplyStatus, QueuedProposal, Record, Status
from sfdq.policy import PolicyViolation, WritePolicy
from sfdq.queue.store import QueueStore
from sfdq.salesforce.base import SalesforceClient, SalesforceError
from sfdq.scanners.base import record_label
from sfdq.util import is_blank


class Outcome(StrEnum):
    """What happened to one proposal during an apply run."""

    DRY_RUN = "dry_run"
    APPLIED = "applied"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True)
class Operation:
    """One Salesforce write the proposal translates to."""

    kind: str  # update | merge | create_task
    object_type: str
    record_id: str | None
    payload: dict[str, Any]
    description: str


@dataclass
class ApplyResult:
    """Outcome of applying (or dry-running) one proposal."""

    proposal_id: int
    outcome: Outcome
    message: str
    operations: list[Operation] = field(default_factory=list)


class NotApprovedError(RuntimeError):
    """Raised if something tries to apply a proposal that a human has not approved."""


def _same(a: object, b: object) -> bool:
    return (is_blank(a) and is_blank(b)) or a == b


class Applier:
    """Applies approved proposals to Salesforce (or just plans them, for ``--dry-run``)."""

    def __init__(
        self,
        store: QueueStore,
        sf: SalesforceClient,
        policy: WritePolicy,
        today: date | None = None,
    ) -> None:
        self._store = store
        self._sf = sf
        self._policy = policy
        self._today = today or date.today()

    # -- public ----------------------------------------------------------------------------
    def apply_approved(
        self,
        *,
        dry_run: bool,
        actor: str,
        limit: int | None = None,
        proposal_ids: list[int] | None = None,
    ) -> list[ApplyResult]:
        """Apply every approved, not-yet-applied proposal (oldest first)."""
        queue = self._store.approved_unapplied(proposal_ids)
        if limit is not None:
            queue = queue[:limit]
        return [self.apply_one(p, dry_run=dry_run, actor=actor) for p in queue]

    def apply_one(self, proposal: QueuedProposal, *, dry_run: bool, actor: str) -> ApplyResult:
        """Apply (or dry-run) a single proposal. Never raises for expected failure modes."""
        # Re-read from the DB: the object we were handed may be stale, and approval is the gate.
        fresh = self._store.get_proposal(proposal.id)
        if fresh.status is not Status.APPROVED:
            raise NotApprovedError(f"proposal {fresh.id} is {fresh.status.value}, not approved")
        if fresh.applied:
            return ApplyResult(fresh.id, Outcome.APPLIED, "already applied; nothing to do")

        try:
            self._policy.check_proposal(fresh.finding, fresh)
        except PolicyViolation as exc:
            return self._finish(
                fresh, Outcome.FAILED, f"policy violation: {exc}", [], dry_run, actor
            )

        try:
            conflict = self._preflight(fresh)
            if conflict:
                return self._finish(fresh, Outcome.CONFLICT, conflict, [], dry_run, actor)
            operations = self._plan(fresh)
        except SalesforceError as exc:
            return self._finish(
                fresh, Outcome.FAILED, f"could not read Salesforce: {exc}", [], dry_run, actor
            )

        if dry_run:
            return self._finish(
                fresh,
                Outcome.DRY_RUN,
                f"would run {len(operations)} operation(s)",
                operations,
                True,
                actor,
            )

        done: list[Operation] = []
        try:
            for op in operations:
                self._execute(op)
                done.append(op)
        except SalesforceError as exc:
            message = f"{exc} (completed {len(done)}/{len(operations)} operations before failing)"
            return self._finish(fresh, Outcome.FAILED, message, done, False, actor)
        return self._finish(
            fresh, Outcome.APPLIED, f"ran {len(done)} operation(s)", done, False, actor
        )

    # -- preflight -------------------------------------------------------------------------
    def _preflight(self, p: QueuedProposal) -> str | None:
        """Return a human-readable conflict message, or ``None`` if it is safe to proceed."""
        needed = (
            list(p.finding.record_ids)
            if p.merge is None
            else [p.merge.survivor_id, *p.merge.victim_ids]
        )
        current: dict[str, Record] = {}
        for record_id in needed:
            record = self._sf.get_record(p.finding.object_type, record_id)
            if record is None:
                return f"{p.finding.object_type} {record_id} no longer exists (deleted or merged)"
            current[record_id] = record
        for change in p.changes:
            actual = current[change.record_id].get(change.field)
            if not _same(actual, change.before):
                return (
                    f"{change.field} on {change.record_id} changed since the proposal was made "
                    f"(expected {change.before!r}, found {actual!r})"
                )
        return None

    # -- planning --------------------------------------------------------------------------
    def _plan(self, p: QueuedProposal) -> list[Operation]:
        obj = p.finding.object_type
        ops: list[Operation] = []
        by_record: dict[str, dict[str, Any]] = {}
        for change in p.changes:
            by_record.setdefault(change.record_id, {})[change.field] = change.after
        for record_id, fields in by_record.items():
            changes = ", ".join(f"{k} -> {v!r}" for k, v in fields.items())
            ops.append(
                Operation("update", obj, record_id, fields, f"Update {obj} {record_id}: {changes}")
            )

        if p.action is ActionType.MERGE and p.merge is not None:
            ops.append(
                Operation(
                    "merge",
                    obj,
                    p.merge.survivor_id,
                    {"victim_ids": list(p.merge.victim_ids)},
                    f"Merge {obj} {', '.join(p.merge.victim_ids)} into {p.merge.survivor_id}",
                )
            )
        elif p.action is ActionType.FLAG_FOR_REVIEW:
            ops.append(self._task_operation(p))
        return ops

    def _task_operation(self, p: QueuedProposal) -> Operation:
        obj = p.finding.object_type
        target_id = p.finding.record_ids[0]
        target = self._sf.get_record(obj, target_id) or {}
        label = record_label(obj, {"Id": target_id, **target})
        others = [i for i in p.finding.record_ids if i != target_id]
        body = [
            p.explanation,
            "",
            f"Reviewer note: {p.review_note}",
            f"Finding: {p.finding.summary}",
        ]
        if others:
            body.append(f"Related records: {', '.join(others)}")
        body.append(f"Proposed by sfdq; approved by {p.decided_by or 'unknown'}.")
        payload: dict[str, Any] = {
            "Subject": f"[Data quality] Review {obj}: {label}"[:255],
            "Description": "\n".join(body),
            "OwnerId": target.get("OwnerId"),
            "WhoId" if obj == "Contact" else "WhatId": target_id,
            "ActivityDate": (self._today + timedelta(days=7)).isoformat(),
            "Status": "Not Started",
            "Priority": "Normal",
        }
        return Operation(
            "create_task",
            "Task",
            None,
            payload,
            f"Create review Task for {obj} {target_id} ({label})",
        )

    # -- execution -------------------------------------------------------------------------
    def _execute(self, op: Operation) -> None:
        if op.kind == "update" and op.record_id:
            self._sf.update_record(op.object_type, op.record_id, op.payload)
        elif op.kind == "merge" and op.record_id:
            self._sf.merge_records(op.object_type, op.record_id, op.payload["victim_ids"])
        elif op.kind == "create_task":
            self._sf.create_record("Task", op.payload)
        else:  # pragma: no cover - programmer error
            raise SalesforceError(f"unknown operation {op.kind}")

    # -- bookkeeping -----------------------------------------------------------------------
    def _finish(
        self,
        p: QueuedProposal,
        outcome: Outcome,
        message: str,
        operations: list[Operation],
        dry_run: bool,
        actor: str,
    ) -> ApplyResult:
        details = {
            "message": message,
            "operations": [
                {
                    "kind": o.kind,
                    "object": o.object_type,
                    "record_id": o.record_id,
                    "payload": o.payload,
                }
                for o in operations
            ],
        }
        if dry_run:
            self._store.log_event(
                actor, "apply_dry_run", p.id, {**details, "outcome": outcome.value}
            )
        else:
            status = {
                Outcome.APPLIED: ApplyStatus.APPLIED,
                Outcome.CONFLICT: ApplyStatus.CONFLICT,
                Outcome.FAILED: ApplyStatus.FAILED,
            }[outcome]
            self._store.record_apply(
                p.id, status, actor, details, error=None if outcome is Outcome.APPLIED else message
            )
        return ApplyResult(p.id, outcome, message, operations)
