"""View-models: turn queue rows into exactly what the templates need (all escaping is Jinja's)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sfdq.models import ActionType, QueuedProposal, Status
from sfdq.scanners.base import record_label
from sfdq.util import is_blank

# Columns shown in the side-by-side duplicate comparison.
COMPARE_FIELDS = {
    "Account": [
        "Name",
        "Website",
        "Phone",
        "Industry",
        "BillingCity",
        "BillingPostalCode",
        "OwnerId",
    ],
    "Contact": ["FirstName", "LastName", "Email", "Phone", "Title", "AccountId", "OwnerId"],
}

KIND_LABELS = {
    "duplicate": "Duplicate",
    "missing_fields": "Missing fields",
    "stale": "Stale",
    "invalid_format": "Invalid format",
}
ACTION_LABELS = {
    ActionType.MERGE: "Merge",
    ActionType.UPDATE_FIELD: "Update field",
    ActionType.FLAG_FOR_REVIEW: "Flag for owner review",
}


@dataclass
class ChangeRow:
    """One before/after line."""

    record: str
    field: str
    before: str
    after: str


@dataclass
class CompareColumn:
    """One record in a duplicate comparison."""

    record_id: str
    role: str  # "keep" | "merge away"
    values: dict[str, str]
    updates: dict[str, str] = field(default_factory=dict)


@dataclass
class CardView:
    """Everything the proposal card template renders."""

    p: QueuedProposal
    kind_label: str
    action_label: str
    state: str  # pending | approved | rejected | applied | failed | conflict
    changes: list[ChangeRow]
    compare_fields: list[str]
    columns: list[CompareColumn]
    evidence: list[str]
    can_decide: bool
    can_undo: bool
    error: str | None = None


def _show(value: Any) -> str:
    return "(blank)" if is_blank(value) else str(value)


def card_view(p: QueuedProposal, error: str | None = None) -> CardView:
    """Build the view for one proposal."""
    obj = p.finding.object_type
    snapshot = p.finding.snapshot

    def label(record_id: str) -> str:
        rec = snapshot.get(record_id, {"Id": record_id})
        return record_label(obj, {"Id": record_id, **rec})

    changes = [
        ChangeRow(label(c.record_id), c.field, _show(c.before), _show(c.after)) for c in p.changes
    ]

    columns: list[CompareColumn] = []
    fields = COMPARE_FIELDS[obj]
    if p.merge:
        updates_by_record: dict[str, dict[str, str]] = {}
        for c in p.changes:
            updates_by_record.setdefault(c.record_id, {})[c.field] = _show(c.after)
        for record_id, role in [
            (p.merge.survivor_id, "keep"),
            *((v, "merge away") for v in p.merge.victim_ids),
        ]:
            rec = snapshot.get(record_id, {})
            columns.append(
                CompareColumn(
                    record_id=record_id,
                    role=role,
                    values={f: _show(rec.get(f)) for f in fields},
                    updates=updates_by_record.get(record_id, {}),
                )
            )

    evidence: list[str] = []
    for pair in p.finding.details.get("pairs", []):
        evidence.extend(e for e in pair["evidence"] if e not in evidence)

    if p.applied:
        state = "applied"
    elif p.apply_status is not None:
        state = p.apply_status.value
    else:
        state = p.status.value
    return CardView(
        p=p,
        kind_label=KIND_LABELS[p.finding.kind.value],
        action_label=ACTION_LABELS[p.action],
        state=state,
        changes=changes,
        compare_fields=fields,
        columns=columns,
        evidence=evidence,
        can_decide=p.status is Status.PENDING,
        can_undo=p.status is not Status.PENDING and not p.applied,
        error=error,
    )


def short_time(iso: str | None) -> str:
    """'2026-11-04T09:30:12+00:00' -> '2026-11-04 09:30 UTC'."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso


def pretty_json(value: Any) -> str:
    """Stable, indented JSON for the evidence drawer."""
    import json

    return json.dumps(value, indent=2, sort_keys=True, default=str, ensure_ascii=False)
