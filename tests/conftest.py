"""Shared fixtures. Everything runs against the mock org -- no Salesforce, no API key."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from sfdq.rules import Rules, load_rules
from sfdq.salesforce.mock import MockSalesforceClient
from sfdq.settings import Settings, load_settings

AS_OF = date(2026, 11, 4)


@pytest.fixture(scope="session")
def rules() -> Rules:
    return load_rules()


@pytest.fixture
def mock_sf() -> MockSalesforceClient:
    """A fresh in-memory mock org (not persisted)."""
    return MockSalesforceClient.load_or_create(None, as_of=AS_OF)


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    """Mock-mode settings rooted in a temp dir, immune to the developer's environment."""
    yield load_settings(
        {"SFDQ_HOME": str(tmp_path / "sfdq"), "SFDQ_MODE": "mock", "SFDQ_REVIEWER": "tester"}
    )


def rec(**fields: object) -> dict[str, object]:
    """Tiny record builder for hand-written scanner cases."""
    return {"Id": fields.pop("Id", "X1"), **fields}


# ---- queue / apply helpers ------------------------------------------------------------------
from sfdq.models import (  # noqa: E402
    ActionType,
    Confidence,
    FieldChange,
    Finding,
    FindingKind,
    MergePlan,
    ProposalDraft,
)
from sfdq.queue import QueueStore  # noqa: E402
from sfdq.scanners import run_scan  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> QueueStore:
    return QueueStore(tmp_path / "queue.db")


@pytest.fixture
def scanned(store: QueueStore, mock_sf: MockSalesforceClient, rules: Rules) -> list[Finding]:
    """Run a scan and persist its findings; returns them."""
    report = run_scan(mock_sf, rules, as_of=AS_OF)
    store.record_scan(report.findings, actor="test")
    return report.findings


def first_finding(
    findings: list[Finding], kind: FindingKind, object_type: str = "Account"
) -> Finding:
    return next(f for f in findings if f.kind is kind and f.object_type == object_type)


def format_fix_draft(finding: Finding, after: str | None = None) -> ProposalDraft:
    """An update_field proposal that fixes an invalid-format finding using its suggestion."""
    d = finding.details
    record_id = finding.record_ids[0]
    return ProposalDraft(
        finding_id=finding.id,
        action=ActionType.UPDATE_FIELD,
        confidence=Confidence.HIGH,
        explanation="Fix the format.",
        changes=[
            FieldChange(
                record_id=record_id,
                field=d["field"],
                before=d["value"],
                after=after or d["suggestion"],
            )
        ],
        source="test",
        model="test",
    )


def merge_draft(finding: Finding) -> ProposalDraft:
    survivor, *victims = finding.record_ids
    return ProposalDraft(
        finding_id=finding.id,
        action=ActionType.MERGE,
        confidence=Confidence.HIGH,
        explanation="Same company.",
        merge=MergePlan(survivor_id=survivor, victim_ids=victims),
        source="test",
        model="test",
    )


def flag_draft(
    finding: Finding, note: str = "Please confirm this record is still needed."
) -> ProposalDraft:
    return ProposalDraft(
        finding_id=finding.id,
        action=ActionType.FLAG_FOR_REVIEW,
        confidence=Confidence.MEDIUM,
        explanation="Looks stale.",
        review_note=note,
        source="test",
        model="test",
    )
