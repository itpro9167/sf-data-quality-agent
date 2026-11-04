"""Domain models shared by scanners, the agent, the queue and the web UI.

These are plain Pydantic models so they validate on the way in (LLM output, database rows) and
serialize to JSON on the way out (SQLite columns, dashboard, audit log).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

Record = dict[str, Any]
"""A Salesforce record as a flat ``{field: value}`` dict (the ``attributes`` key is stripped)."""

ObjectType = Literal["Account", "Contact"]
OBJECT_TYPES: tuple[ObjectType, ...] = ("Account", "Contact")


class FindingKind(StrEnum):
    """The four deterministic checks."""

    DUPLICATE = "duplicate"
    MISSING_FIELDS = "missing_fields"
    STALE = "stale"
    INVALID_FORMAT = "invalid_format"


class ActionType(StrEnum):
    """What a proposal asks a human to approve."""

    MERGE = "merge"
    UPDATE_FIELD = "update_field"
    FLAG_FOR_REVIEW = "flag_for_review"


class Confidence(StrEnum):
    """The agent's own confidence in its proposal."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Status(StrEnum):
    """Review state of a queued proposal."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApplyStatus(StrEnum):
    """Outcome of the last attempt to apply an approved proposal."""

    APPLIED = "applied"
    FAILED = "failed"
    CONFLICT = "conflict"


class Finding(BaseModel):
    """A single data-quality issue found by a deterministic scanner.

    ``id`` is derived from the issue's identity, so re-scanning an unchanged org yields the
    same IDs and the queue never fills with duplicates of the same finding.
    """

    id: str
    kind: FindingKind
    object_type: ObjectType
    record_ids: list[str] = Field(min_length=1)
    summary: str
    score: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    snapshot: dict[str, Record] = Field(default_factory=dict)


class FieldChange(BaseModel):
    """A before/after value for one field on one record."""

    record_id: str
    field: str
    before: Any = None
    after: Any = None


class MergePlan(BaseModel):
    """Merge ``victim_ids`` into ``survivor_id`` (same object type)."""

    survivor_id: str
    victim_ids: list[str] = Field(min_length=1)


class ProposalDraft(BaseModel):
    """A fix proposed by the agent. Nothing here has touched Salesforce.

    For ``update_field`` the ``changes`` are the edits. For ``merge`` the ``changes`` are
    optional edits to the surviving record (e.g. copying a phone number from a victim).
    For ``flag_for_review`` the ``review_note`` becomes a Task for the record owner.
    """

    finding_id: str
    action: ActionType
    confidence: Confidence
    explanation: str = Field(min_length=1)
    changes: list[FieldChange] = Field(default_factory=list)
    merge: MergePlan | None = None
    review_note: str | None = None
    source: str = "unknown"
    model: str = "unknown"

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.action is ActionType.MERGE:
            if self.merge is None:
                raise ValueError("merge proposals require a merge plan")
            if self.merge.survivor_id in self.merge.victim_ids:
                raise ValueError("merge survivor cannot also be a victim")
            if any(c.record_id != self.merge.survivor_id for c in self.changes):
                raise ValueError("merge changes may only edit the surviving record")
        elif self.merge is not None:
            raise ValueError("only merge proposals may carry a merge plan")
        if self.action is ActionType.UPDATE_FIELD and not self.changes:
            raise ValueError("update_field proposals require at least one change")
        if self.action is ActionType.FLAG_FOR_REVIEW and not (self.review_note or "").strip():
            raise ValueError("flag_for_review proposals require a review note")
        return self


class QueuedProposal(ProposalDraft):
    """A proposal as stored in the review queue, with its finding and review/apply state."""

    id: int
    status: Status = Status.PENDING
    created_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    apply_status: ApplyStatus | None = None
    applied_at: str | None = None
    apply_error: str | None = None
    finding: Finding

    @property
    def applied(self) -> bool:
        """True once the proposal has been successfully written to Salesforce."""
        return self.apply_status is ApplyStatus.APPLIED


class AuditEntry(BaseModel):
    """One row of the append-only, hash-chained audit log."""

    id: int
    ts: str
    actor: str
    event: str
    proposal_id: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str
