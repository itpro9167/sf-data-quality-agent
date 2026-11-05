"""SQLite-backed findings store, human review queue and audit-log access.

Every state change (scan, proposal created, approve/reject/reopen, apply outcome) is written
together with its audit-log entry in ONE transaction, so the log can never disagree with the data.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sfdq.models import (
    ApplyStatus,
    AuditEntry,
    Confidence,
    Finding,
    FindingKind,
    ProposalDraft,
    QueuedProposal,
    Status,
)
from sfdq.queue import audit
from sfdq.queue.audit import ChainStatus
from sfdq.util import utcnow_iso

SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS findings (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    object_type   TEXT NOT NULL,
    record_ids    TEXT NOT NULL,
    summary       TEXT NOT NULL,
    score         REAL,
    details       TEXT NOT NULL,
    snapshot      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    last_scan_id  TEXT NOT NULL,
    resolved_at   TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id    TEXT NOT NULL UNIQUE REFERENCES findings(id),
    action        TEXT NOT NULL CHECK (action IN ('merge', 'update_field', 'flag_for_review')),
    confidence    TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    explanation   TEXT NOT NULL,
    changes       TEXT NOT NULL,
    merge         TEXT,
    review_note   TEXT,
    source        TEXT NOT NULL,
    model         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at    TEXT NOT NULL,
    decided_at    TEXT,
    decided_by    TEXT,
    decision_note TEXT,
    apply_status  TEXT CHECK (apply_status IN ('applied', 'failed', 'conflict')),
    applied_at    TEXT,
    apply_error   TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
"""
    + audit.SCHEMA
)


class QueueError(RuntimeError):
    """Base class for queue errors."""


class NotFoundError(QueueError):
    """The requested proposal or finding does not exist."""


class InvalidTransition(QueueError):
    """The requested status change is not allowed from the current state."""


class DuplicateProposal(QueueError):
    """A proposal already exists for this finding."""


@dataclass(frozen=True)
class ScanDiff:
    """How a scan changed the findings table."""

    new: int
    unchanged: int
    resolved: int
    reopened: int = 0


@dataclass(frozen=True)
class FindingRow:
    """A finding plus the state of its proposal, if it has one."""

    finding: Finding
    proposal_id: int | None
    proposal_status: Status | None


class QueueStore:
    """Findings, proposals and the audit log, in one SQLite file."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # -- connections -----------------------------------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # -- findings --------------------------------------------------------------------------
    def record_scan(
        self,
        findings: Sequence[Finding],
        actor: str,
        kinds: Iterable[FindingKind] | None = None,
    ) -> ScanDiff:
        """Upsert ``findings`` and mark previously-open findings that vanished as resolved.

        ``kinds`` limits which finding kinds may be resolved (a partial scan must not resolve
        kinds it did not look at).
        """
        scanned_kinds = [k.value for k in (kinds if kinds is not None else FindingKind)]
        scan_id = uuid.uuid4().hex[:8]
        now = utcnow_iso()
        new = unchanged = reopened = 0
        with self._tx() as conn:
            for f in findings:
                existing = conn.execute(
                    "SELECT resolved_at FROM findings WHERE id = ?", (f.id,)
                ).fetchone()
                if existing is None:
                    new += 1
                elif existing["resolved_at"] is not None:
                    reopened += 1
                else:
                    unchanged += 1
                conn.execute(
                    """
                    INSERT INTO findings (id, kind, object_type, record_ids, summary, score,
                        details, snapshot, first_seen_at, last_seen_at, last_scan_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        summary = excluded.summary, score = excluded.score,
                        details = excluded.details, snapshot = excluded.snapshot,
                        last_seen_at = excluded.last_seen_at, last_scan_id = excluded.last_scan_id,
                        resolved_at = NULL
                    """,
                    (
                        f.id,
                        f.kind.value,
                        f.object_type,
                        json.dumps(f.record_ids),
                        f.summary,
                        f.score,
                        json.dumps(f.details, default=str),
                        json.dumps(f.snapshot, default=str),
                        now,
                        now,
                        scan_id,
                    ),
                )
            placeholders = ",".join("?" for _ in scanned_kinds)
            cur = conn.execute(
                f"UPDATE findings SET resolved_at = ? WHERE resolved_at IS NULL "
                f"AND last_scan_id != ? AND kind IN ({placeholders})",
                (now, scan_id, *scanned_kinds),
            )
            resolved = cur.rowcount
            audit.append(
                conn,
                actor,
                "scan_completed",
                None,
                {
                    "scan_id": scan_id,
                    "new": new,
                    "unchanged": unchanged,
                    "reopened": reopened,
                    "resolved": resolved,
                },
            )
        return ScanDiff(new=new, unchanged=unchanged, resolved=resolved, reopened=reopened)

    def get_finding(self, finding_id: str) -> Finding:
        """Fetch one finding, or raise :class:`NotFoundError`."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"finding {finding_id} not found")
        return _finding_from_row(row)

    def findings_without_proposal(
        self, kinds: Iterable[FindingKind] | None = None, limit: int | None = None
    ) -> list[Finding]:
        """Open findings that have no proposal yet, highest-scoring / most actionable first."""
        sql = (
            "SELECT f.* FROM findings f LEFT JOIN proposals p ON p.finding_id = f.id "
            "WHERE p.id IS NULL AND f.resolved_at IS NULL"
        )
        params: list[Any] = []
        if kinds is not None:
            values = [k.value for k in kinds]
            sql += f" AND f.kind IN ({','.join('?' for _ in values)})"
            params += values
        sql += " ORDER BY f.kind, COALESCE(f.score, 0) DESC, f.id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            return [_finding_from_row(r) for r in conn.execute(sql, params)]

    def list_findings(
        self,
        kind: FindingKind | None = None,
        object_type: str | None = None,
        proposal: str = "any",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[FindingRow], int]:
        """Open findings with their proposal state, plus the total matching count.

        Args:
            kind: Only this finding kind.
            object_type: Only ``Account`` or ``Contact``.
            proposal: ``"any"``, ``"none"`` (no proposal yet) or ``"exists"``.
            limit: Page size.
            offset: Rows to skip.
        """
        if proposal not in ("any", "none", "exists"):
            raise ValueError("proposal must be 'any', 'none' or 'exists'")
        where = "f.resolved_at IS NULL"
        params: list[Any] = []
        if kind is not None:
            where += " AND f.kind = ?"
            params.append(kind.value)
        if object_type is not None:
            where += " AND f.object_type = ?"
            params.append(object_type)
        if proposal == "none":
            where += " AND p.id IS NULL"
        elif proposal == "exists":
            where += " AND p.id IS NOT NULL"
        base = f"FROM findings f LEFT JOIN proposals p ON p.finding_id = f.id WHERE {where}"
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS n {base}", params).fetchone()["n"]
            rows = conn.execute(
                f"SELECT f.*, p.id AS p_id, p.status AS p_status {base} "
                "ORDER BY f.kind, COALESCE(f.score, 0) DESC, f.id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return (
            [
                FindingRow(
                    _finding_from_row(r),
                    r["p_id"],
                    Status(r["p_status"]) if r["p_status"] else None,
                )
                for r in rows
            ],
            int(total),
        )

    def count_findings(self, *, open_only: bool = True) -> dict[str, int]:
        """Findings per kind."""
        sql = "SELECT kind, COUNT(*) AS n FROM findings"
        if open_only:
            sql += " WHERE resolved_at IS NULL"
        sql += " GROUP BY kind"
        with self._connect() as conn:
            return {r["kind"]: r["n"] for r in conn.execute(sql)}

    # -- proposals -------------------------------------------------------------------------
    def add_proposal(self, draft: ProposalDraft, actor: str) -> int:
        """Queue a proposal as ``pending`` and audit it. Raises :class:`DuplicateProposal`."""
        try:
            with self._tx() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO proposals (finding_id, action, confidence, explanation, changes,
                        merge, review_note, source, model, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.finding_id,
                        draft.action.value,
                        draft.confidence.value,
                        draft.explanation,
                        json.dumps([c.model_dump() for c in draft.changes], default=str),
                        draft.merge.model_dump_json() if draft.merge else None,
                        draft.review_note,
                        draft.source,
                        draft.model,
                        utcnow_iso(),
                    ),
                )
                proposal_id = int(cur.lastrowid or 0)
                audit.append(
                    conn,
                    actor,
                    "proposal_created",
                    proposal_id,
                    {
                        "finding_id": draft.finding_id,
                        "action": draft.action.value,
                        "confidence": draft.confidence.value,
                        "source": draft.source,
                        "model": draft.model,
                    },
                )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise DuplicateProposal(
                    f"finding {draft.finding_id} already has a proposal"
                ) from exc
            raise
        return proposal_id

    def get_proposal(self, proposal_id: int) -> QueuedProposal:
        """Fetch one proposal with its finding, or raise :class:`NotFoundError`."""
        with self._connect() as conn:
            return self._get(conn, proposal_id)

    def list_proposals(
        self,
        status: Status | None = None,
        kind: FindingKind | None = None,
        confidence: Confidence | None = None,
        object_type: str | None = None,
    ) -> list[QueuedProposal]:
        """List proposals (oldest first), optionally filtered."""
        sql = _PROPOSAL_SELECT + " WHERE 1 = 1"
        params: list[Any] = []
        for column, value in (
            ("p.status", status.value if status else None),
            ("f.kind", kind.value if kind else None),
            ("p.confidence", confidence.value if confidence else None),
            ("f.object_type", object_type),
        ):
            if value is not None:
                sql += f" AND {column} = ?"
                params.append(value)
        sql += " ORDER BY p.id"
        with self._connect() as conn:
            return [_proposal_from_row(r) for r in conn.execute(sql, params)]

    def approved_unapplied(self, proposal_ids: Sequence[int] | None = None) -> list[QueuedProposal]:
        """Approved proposals that have not yet been successfully applied."""
        sql = (
            _PROPOSAL_SELECT + " WHERE p.status = 'approved' "
            "AND (p.apply_status IS NULL OR p.apply_status != 'applied')"
        )
        params: list[Any] = []
        if proposal_ids is not None:
            sql += f" AND p.id IN ({','.join('?' for _ in proposal_ids)})"
            params += list(proposal_ids)
        sql += " ORDER BY p.id"
        with self._connect() as conn:
            return [_proposal_from_row(r) for r in conn.execute(sql, params)]

    def counts(self) -> dict[str, int]:
        """Dashboard counters: pending / approved / rejected / applied / awaiting_apply."""
        with self._connect() as conn:
            by_status = {
                r["status"]: r["n"]
                for r in conn.execute("SELECT status, COUNT(*) AS n FROM proposals GROUP BY status")
            }
            applied = conn.execute(
                "SELECT COUNT(*) AS n FROM proposals WHERE apply_status = 'applied'"
            ).fetchone()["n"]
        approved = by_status.get("approved", 0)
        return {
            "pending": by_status.get("pending", 0),
            "approved": approved,
            "rejected": by_status.get("rejected", 0),
            "applied": applied,
            "awaiting_apply": approved - applied,
        }

    # -- review decisions ------------------------------------------------------------------
    def decide(
        self, proposal_id: int, status: Status, actor: str, note: str | None = None
    ) -> QueuedProposal:
        """Approve or reject a ``pending`` proposal."""
        if status not in (Status.APPROVED, Status.REJECTED):
            raise ValueError("decide() takes APPROVED or REJECTED")
        with self._tx() as conn:
            current = self._get(conn, proposal_id)
            if current.status is not Status.PENDING:
                raise InvalidTransition(
                    f"proposal {proposal_id} is {current.status.value}; only pending proposals "
                    "can be approved or rejected"
                )
            conn.execute(
                "UPDATE proposals SET status = ?, decided_at = ?, decided_by = ?, "
                "decision_note = ? WHERE id = ?",
                (status.value, utcnow_iso(), actor, (note or "").strip() or None, proposal_id),
            )
            audit.append(
                conn,
                actor,
                f"proposal_{status.value}",
                proposal_id,
                {"note": (note or "").strip() or None, "action": current.action.value},
            )
            return self._get(conn, proposal_id)

    def reopen(self, proposal_id: int, actor: str) -> QueuedProposal:
        """Send an approved/rejected (not yet applied) proposal back to ``pending``."""
        with self._tx() as conn:
            current = self._get(conn, proposal_id)
            if current.status is Status.PENDING:
                raise InvalidTransition(f"proposal {proposal_id} is already pending")
            if current.applied:
                raise InvalidTransition(f"proposal {proposal_id} was already applied")
            conn.execute(
                "UPDATE proposals SET status = 'pending', decided_at = NULL, decided_by = NULL, "
                "decision_note = NULL, apply_status = NULL, apply_error = NULL WHERE id = ?",
                (proposal_id,),
            )
            audit.append(
                conn, actor, "proposal_reopened", proposal_id, {"was": current.status.value}
            )
            return self._get(conn, proposal_id)

    # -- apply outcomes --------------------------------------------------------------------
    def record_apply(
        self,
        proposal_id: int,
        outcome: ApplyStatus,
        actor: str,
        details: dict[str, Any],
        error: str | None = None,
    ) -> None:
        """Persist the result of a real (non-dry-run) apply attempt and audit it."""
        with self._tx() as conn:
            current = self._get(conn, proposal_id)
            if current.status is not Status.APPROVED:
                raise InvalidTransition(f"proposal {proposal_id} is not approved")
            conn.execute(
                "UPDATE proposals SET apply_status = ?, apply_error = ?, applied_at = ? "
                "WHERE id = ?",
                (
                    outcome.value,
                    error,
                    utcnow_iso() if outcome is ApplyStatus.APPLIED else None,
                    proposal_id,
                ),
            )
            audit.append(
                conn,
                actor,
                f"apply_{'succeeded' if outcome is ApplyStatus.APPLIED else outcome.value}",
                proposal_id,
                {**details, "error": error},
            )

    # -- audit log -------------------------------------------------------------------------
    def log_event(
        self,
        actor: str,
        event: str,
        proposal_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Append a free-standing audit entry (e.g. ``apply_dry_run``)."""
        with self._tx() as conn:
            return audit.append(conn, actor, event, proposal_id, details)

    def audit_entries(self, limit: int = 200, proposal_id: int | None = None) -> list[AuditEntry]:
        """Most recent audit entries first."""
        sql = "SELECT * FROM audit_log"
        params: list[Any] = []
        if proposal_id is not None:
            sql += " WHERE proposal_id = ?"
            params.append(proposal_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            return [audit.row_to_entry(r) for r in conn.execute(sql, params)]

    def verify_audit_chain(self) -> ChainStatus:
        """Check the audit log's hash chain end to end."""
        with self._connect() as conn:
            return audit.verify_chain(conn)

    # -- internals -------------------------------------------------------------------------
    def _get(self, conn: sqlite3.Connection, proposal_id: int) -> QueuedProposal:
        row = conn.execute(_PROPOSAL_SELECT + " WHERE p.id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"proposal {proposal_id} not found")
        return _proposal_from_row(row)


_PROPOSAL_SELECT = """
SELECT p.*, f.kind AS f_kind, f.object_type AS f_object_type, f.record_ids AS f_record_ids,
       f.summary AS f_summary, f.score AS f_score, f.details AS f_details,
       f.snapshot AS f_snapshot
FROM proposals p JOIN findings f ON f.id = p.finding_id
"""


def _finding_from_row(row: sqlite3.Row) -> Finding:
    return Finding(
        id=row["id"],
        kind=FindingKind(row["kind"]),
        object_type=row["object_type"],
        record_ids=json.loads(row["record_ids"]),
        summary=row["summary"],
        score=row["score"],
        details=json.loads(row["details"]),
        snapshot=json.loads(row["snapshot"]),
    )


def _proposal_from_row(row: sqlite3.Row) -> QueuedProposal:
    finding = Finding(
        id=row["finding_id"],
        kind=FindingKind(row["f_kind"]),
        object_type=row["f_object_type"],
        record_ids=json.loads(row["f_record_ids"]),
        summary=row["f_summary"],
        score=row["f_score"],
        details=json.loads(row["f_details"]),
        snapshot=json.loads(row["f_snapshot"]),
    )
    return QueuedProposal(
        id=row["id"],
        finding_id=row["finding_id"],
        action=row["action"],
        confidence=row["confidence"],
        explanation=row["explanation"],
        changes=json.loads(row["changes"]),
        merge=json.loads(row["merge"]) if row["merge"] else None,
        review_note=row["review_note"],
        source=row["source"],
        model=row["model"],
        status=row["status"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        decision_note=row["decision_note"],
        apply_status=row["apply_status"],
        applied_at=row["applied_at"],
        apply_error=row["apply_error"],
        finding=finding,
    )
