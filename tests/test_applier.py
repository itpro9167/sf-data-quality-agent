"""The apply step: approval gate, dry-run, conflict detection, execution and audit trail."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sfdq.models import ApplyStatus, Finding, FindingKind, Status
from sfdq.policy import WritePolicy
from sfdq.queue import Applier, NotApprovedError, Outcome, QueueStore
from sfdq.rules import Rules
from sfdq.salesforce.base import SalesforceError
from sfdq.salesforce.mock import MockSalesforceClient
from tests.conftest import AS_OF, first_finding, flag_draft, format_fix_draft, merge_draft


@pytest.fixture
def applier(store: QueueStore, mock_sf: MockSalesforceClient, rules: Rules) -> Applier:
    return Applier(store, mock_sf, WritePolicy(rules), today=AS_OF)


def _fixable(scanned: list[Finding]) -> Finding:
    return next(
        f for f in scanned
        if f.kind is FindingKind.INVALID_FORMAT and f.details["suggestion"] and f.object_type == "Account"
    )  # fmt: skip


def _queue_approved(store: QueueStore, draft) -> int:
    pid = store.add_proposal(draft, "agent")
    store.decide(pid, Status.APPROVED, "alice")
    return pid


def test_pending_and_rejected_proposals_are_never_applied(
    store: QueueStore, applier: Applier, mock_sf: MockSalesforceClient, scanned: list[Finding]
) -> None:
    finding = _fixable(scanned)
    pid = store.add_proposal(format_fix_draft(finding), "agent")
    before = mock_sf.get_record("Account", finding.record_ids[0])

    assert applier.apply_approved(dry_run=False, actor="op") == []  # pending: invisible to apply
    with pytest.raises(NotApprovedError):  # ...and refused even if someone asks directly
        applier.apply_one(store.get_proposal(pid), dry_run=False, actor="op")
    store.decide(pid, Status.REJECTED, "alice")
    assert applier.apply_approved(dry_run=False, actor="op") == []
    assert mock_sf.get_record("Account", finding.record_ids[0]) == before


def test_dry_run_changes_nothing_but_is_audited(
    store: QueueStore, applier: Applier, mock_sf: MockSalesforceClient, scanned: list[Finding]
) -> None:
    finding = _fixable(scanned)
    pid = _queue_approved(store, format_fix_draft(finding))
    before = mock_sf.get_record("Account", finding.record_ids[0])

    (result,) = applier.apply_approved(dry_run=True, actor="op")
    assert result.outcome is Outcome.DRY_RUN
    assert [o.kind for o in result.operations] == ["update"]
    assert mock_sf.get_record("Account", finding.record_ids[0]) == before
    item = store.get_proposal(pid)
    assert (
        item.apply_status is None and item.status is Status.APPROVED
    )  # still awaiting a real apply
    entry = store.audit_entries(proposal_id=pid)[0]
    assert entry.event == "apply_dry_run" and entry.details["operations"][0]["kind"] == "update"
    assert store.verify_audit_chain().ok


def test_apply_update_field_writes_and_marks_applied_once(
    store: QueueStore, applier: Applier, mock_sf: MockSalesforceClient, scanned: list[Finding]
) -> None:
    finding = _fixable(scanned)
    draft = format_fix_draft(finding)
    pid = _queue_approved(store, draft)
    (result,) = applier.apply_approved(dry_run=False, actor="op")

    assert result.outcome is Outcome.APPLIED
    field = finding.details["field"]
    assert mock_sf.get_record("Account", finding.record_ids[0])[field] == draft.changes[0].after
    item = store.get_proposal(pid)
    assert item.apply_status is ApplyStatus.APPLIED and item.applied_at
    assert store.audit_entries(proposal_id=pid)[0].event == "apply_succeeded"
    assert applier.apply_approved(dry_run=False, actor="op") == []  # idempotent: nothing left
    assert store.counts()["applied"] == 1


def test_conflict_when_record_changed_after_the_proposal(
    store: QueueStore, applier: Applier, mock_sf: MockSalesforceClient, scanned: list[Finding]
) -> None:
    finding = _fixable(scanned)
    pid = _queue_approved(store, format_fix_draft(finding))
    field = finding.details["field"]
    mock_sf.update_record(
        "Account", finding.record_ids[0], {field: "(212) 555-0100"}
    )  # a human edit

    (result,) = applier.apply_approved(dry_run=False, actor="op")
    assert result.outcome is Outcome.CONFLICT and "changed since the proposal" in result.message
    assert (
        mock_sf.get_record("Account", finding.record_ids[0])[field] == "(212) 555-0100"
    )  # untouched
    item = store.get_proposal(pid)
    assert item.apply_status is ApplyStatus.CONFLICT and item.apply_error
    assert store.audit_entries(proposal_id=pid)[0].event == "apply_conflict"


def test_conflict_when_record_was_merged_away(
    store: QueueStore, applier: Applier, mock_sf: MockSalesforceClient, scanned: list[Finding]
) -> None:
    dup = first_finding(scanned, FindingKind.DUPLICATE)
    pid = _queue_approved(store, merge_draft(dup))
    mock_sf.merge_records("Account", dup.record_ids[0], dup.record_ids[1:])  # someone beat us to it
    (result,) = applier.apply_approved(dry_run=False, actor="op")
    assert result.outcome is Outcome.CONFLICT and "no longer exists" in result.message
    assert store.get_proposal(pid).apply_status is ApplyStatus.CONFLICT


def test_merge_apply_reparents_children_and_removes_victims(
    store: QueueStore, applier: Applier, mock_sf: MockSalesforceClient, scanned: list[Finding]
) -> None:
    dup = first_finding(scanned, FindingKind.DUPLICATE)
    survivor, *victims = dup.record_ids
    kids = [
        c["Id"] for v in victims for c in mock_sf.query_records("Contact", where={"AccountId": v})
    ]
    _queue_approved(store, merge_draft(dup))
    (result,) = applier.apply_approved(dry_run=False, actor="op")
    assert result.outcome is Outcome.APPLIED and result.operations[-1].kind == "merge"
    assert all(mock_sf.get_record("Account", v) is None for v in victims)
    assert all(mock_sf.get_record("Contact", k)["AccountId"] == survivor for k in kids)


def test_flag_for_review_creates_a_task_for_the_record_owner(
    store: QueueStore, applier: Applier, mock_sf: MockSalesforceClient, scanned: list[Finding]
) -> None:
    finding = first_finding(scanned, FindingKind.STALE, "Contact")
    owner = mock_sf.get_record("Contact", finding.record_ids[0])["OwnerId"]
    _queue_approved(store, flag_draft(finding, "Is this contact still at the company?"))
    (result,) = applier.apply_approved(dry_run=False, actor="op")
    assert result.outcome is Outcome.APPLIED
    (task,) = mock_sf.query_records("Task")
    assert task["OwnerId"] == owner and task["WhoId"] == finding.record_ids[0]
    assert (
        "still at the company" in task["Description"] and "approved by alice" in task["Description"]
    )
    assert task["ActivityDate"] == "2026-11-11"


def test_policy_is_rechecked_at_apply_time(
    store: QueueStore, applier: Applier, mock_sf: MockSalesforceClient, scanned: list[Finding]
) -> None:
    finding = _fixable(scanned)
    pid = _queue_approved(store, format_fix_draft(finding))
    conn = sqlite3.connect(store.db_path)  # someone hand-edits the queue to target OwnerId
    conn.execute(
        "UPDATE proposals SET changes = replace(changes, ?, '\"OwnerId\"') WHERE id = ?",
        (json.dumps(finding.details["field"]), pid),
    )
    conn.commit()
    conn.close()
    before = mock_sf.get_record("Account", finding.record_ids[0])
    (result,) = applier.apply_approved(dry_run=False, actor="op")
    assert result.outcome is Outcome.FAILED and "policy violation" in result.message
    assert mock_sf.get_record("Account", finding.record_ids[0]) == before


def test_salesforce_failure_is_recorded_not_raised(
    store: QueueStore, applier: Applier, mock_sf: MockSalesforceClient, scanned: list[Finding],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    finding = _fixable(scanned)
    pid = _queue_approved(store, format_fix_draft(finding))

    def boom(*_: object, **__: object) -> None:
        raise SalesforceError("REQUEST_LIMIT_EXCEEDED")

    monkeypatch.setattr(mock_sf, "update_record", boom)
    (result,) = applier.apply_approved(dry_run=False, actor="op")
    assert result.outcome is Outcome.FAILED
    item = store.get_proposal(pid)
    assert item.apply_status is ApplyStatus.FAILED and "REQUEST_LIMIT_EXCEEDED" in (
        item.apply_error or ""
    )

    monkeypatch.undo()  # the next run retries failed proposals
    (retry,) = applier.apply_approved(dry_run=False, actor="op")
    assert retry.outcome is Outcome.APPLIED


def test_limit_and_explicit_ids(
    store: QueueStore, applier: Applier, scanned: list[Finding]
) -> None:
    stale = [f for f in scanned if f.kind is FindingKind.STALE][:3]
    ids = [_queue_approved(store, flag_draft(f)) for f in stale]
    assert len(applier.apply_approved(dry_run=True, actor="op", limit=2)) == 2
    only = applier.apply_approved(dry_run=True, actor="op", proposal_ids=[ids[2]])
    assert [r.proposal_id for r in only] == [ids[2]]
