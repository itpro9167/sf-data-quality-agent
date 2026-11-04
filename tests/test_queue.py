"""Queue store: scan upserts, review state machine, and the tamper-evident audit log."""

from __future__ import annotations

import sqlite3

import pytest
from tests.conftest import AS_OF, first_finding, flag_draft, format_fix_draft

from sfdq.models import Finding, FindingKind, Status
from sfdq.queue import (
    DuplicateProposal,
    InvalidTransition,
    NotFoundError,
    QueueStore,
)
from sfdq.rules import Rules
from sfdq.salesforce.mock import MockSalesforceClient
from sfdq.scanners import run_scan


def test_rescan_is_idempotent_and_resolves_vanished_findings(
    store: QueueStore, mock_sf: MockSalesforceClient, rules: Rules
) -> None:
    findings = run_scan(mock_sf, rules, as_of=AS_OF).findings
    first = store.record_scan(findings, "test")
    assert (first.new, first.unchanged, first.resolved) == (len(findings), 0, 0)

    second = store.record_scan(findings, "test")
    assert (second.new, second.unchanged, second.resolved) == (0, len(findings), 0)

    third = store.record_scan(findings[10:], "test")  # ten findings "got fixed"
    assert third.resolved == 10
    assert sum(store.count_findings().values()) == len(findings) - 10

    fourth = store.record_scan(findings, "test")  # ...and they came back
    assert (fourth.new, fourth.reopened) == (0, 10)
    assert sum(store.count_findings().values()) == len(findings)


def test_partial_scan_only_resolves_kinds_it_scanned(
    store: QueueStore, mock_sf: MockSalesforceClient, rules: Rules
) -> None:
    everything = run_scan(mock_sf, rules, as_of=AS_OF).findings
    store.record_scan(everything, "test")
    store.record_scan([], "test", kinds=[FindingKind.STALE])  # a stale-only scan finds nothing
    remaining = store.count_findings()
    assert FindingKind.STALE.value not in remaining
    assert remaining[FindingKind.DUPLICATE.value] > 0


def test_new_proposals_start_pending_and_are_audited(
    store: QueueStore, scanned: list[Finding]
) -> None:
    finding = first_finding(scanned, FindingKind.INVALID_FORMAT)
    pid = store.add_proposal(format_fix_draft(finding, after="x"), "agent")
    item = store.get_proposal(pid)
    assert item.status is Status.PENDING and item.apply_status is None
    assert item.finding.id == finding.id
    events = [e.event for e in store.audit_entries()]
    assert "proposal_created" in events


def test_one_proposal_per_finding(store: QueueStore, scanned: list[Finding]) -> None:
    finding = first_finding(scanned, FindingKind.STALE)
    store.add_proposal(flag_draft(finding), "agent")
    with pytest.raises(DuplicateProposal):
        store.add_proposal(flag_draft(finding), "agent")


def test_findings_without_proposal_excludes_proposed_and_honours_limit(
    store: QueueStore, scanned: list[Finding]
) -> None:
    stale = [f for f in scanned if f.kind is FindingKind.STALE]
    store.add_proposal(flag_draft(stale[0]), "agent")
    todo = store.findings_without_proposal([FindingKind.STALE])
    assert stale[0].id not in {f.id for f in todo} and len(todo) == len(stale) - 1
    assert len(store.findings_without_proposal(limit=3)) == 3


def test_review_state_machine(store: QueueStore, scanned: list[Finding]) -> None:
    pid = store.add_proposal(flag_draft(first_finding(scanned, FindingKind.STALE)), "agent")
    approved = store.decide(pid, Status.APPROVED, "alice", "looks right")
    assert approved.status is Status.APPROVED
    assert approved.decided_by == "alice" and approved.decision_note == "looks right"

    with pytest.raises(InvalidTransition):  # can't decide twice
        store.decide(pid, Status.REJECTED, "bob")
    reopened = store.reopen(pid, "alice")
    assert reopened.status is Status.PENDING and reopened.decided_by is None
    assert store.decide(pid, Status.REJECTED, "bob").status is Status.REJECTED
    with pytest.raises(InvalidTransition):
        store.reopen(
            store.add_proposal(flag_draft(first_finding(scanned, FindingKind.MISSING_FIELDS)), "a"),
            "x",
        )
    with pytest.raises(NotFoundError):
        store.get_proposal(9999)
    with pytest.raises(ValueError):
        store.decide(pid, Status.PENDING, "alice")


def test_counts_and_filters(store: QueueStore, scanned: list[Finding]) -> None:
    stale = [f for f in scanned if f.kind is FindingKind.STALE][:3]
    ids = [store.add_proposal(flag_draft(f), "agent") for f in stale]
    store.decide(ids[0], Status.APPROVED, "a")
    store.decide(ids[1], Status.REJECTED, "a")
    assert store.counts() == {
        "pending": 1,
        "approved": 1,
        "rejected": 1,
        "applied": 0,
        "awaiting_apply": 1,
    }
    assert [p.id for p in store.list_proposals(status=Status.REJECTED)] == [ids[1]]
    assert len(store.list_proposals(kind=FindingKind.STALE)) == 3
    assert store.list_proposals(kind=FindingKind.DUPLICATE) == []


def test_audit_chain_verifies_and_every_state_change_is_logged(
    store: QueueStore, scanned: list[Finding]
) -> None:
    pid = store.add_proposal(flag_draft(first_finding(scanned, FindingKind.STALE)), "agent")
    store.decide(pid, Status.APPROVED, "alice")
    store.reopen(pid, "alice")
    status = store.verify_audit_chain()
    assert status.ok and status.entries >= 4
    events = [e.event for e in reversed(store.audit_entries())]
    assert events == [
        "scan_completed",
        "proposal_created",
        "proposal_approved",
        "proposal_reopened",
    ]


def test_audit_log_rejects_update_and_delete(store: QueueStore, scanned: list[Finding]) -> None:
    conn = sqlite3.connect(store.db_path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_log SET actor = 'mallory'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log")
    conn.close()


def test_audit_chain_detects_tampering_even_if_triggers_are_dropped(
    store: QueueStore, scanned: list[Finding]
) -> None:
    store.add_proposal(flag_draft(first_finding(scanned, FindingKind.STALE)), "agent")
    conn = sqlite3.connect(store.db_path, isolation_level=None)
    conn.execute("DROP TRIGGER audit_log_no_update")
    conn.execute("UPDATE audit_log SET actor = 'mallory' WHERE id = 1")
    conn.close()
    status = store.verify_audit_chain()
    assert not status.ok and status.first_bad_id == 1
