"""The write policy is the last line of defence against bad (or injected) model output."""

from __future__ import annotations

import pytest

from sfdq.models import ActionType, Confidence, FieldChange, Finding, FindingKind, ProposalDraft
from sfdq.policy import PolicyViolation, WritePolicy
from sfdq.rules import Rules
from tests.conftest import first_finding, flag_draft, format_fix_draft, merge_draft


@pytest.fixture
def policy(rules: Rules) -> WritePolicy:
    return WritePolicy(rules)


def _update(
    finding: Finding, field: str, before: object, after: object, record_id: str | None = None
):
    return ProposalDraft(
        finding_id=finding.id,
        action=ActionType.UPDATE_FIELD,
        confidence=Confidence.HIGH,
        explanation="x",
        changes=[
            FieldChange(
                record_id=record_id or finding.record_ids[0],
                field=field,
                before=before,
                after=after,
            )
        ],
    )


def test_accepts_a_valid_fix(policy: WritePolicy, scanned: list[Finding]) -> None:
    finding = next(
        f for f in scanned if f.kind is FindingKind.INVALID_FORMAT and f.details["suggestion"]
    )
    policy.check_proposal(finding, format_fix_draft(finding))


@pytest.mark.parametrize("field", ["OwnerId", "AccountId", "Id", "CreatedDate", "IsDeleted"])
def test_rejects_fields_outside_the_allow_list(
    policy: WritePolicy, scanned: list[Finding], field: str
) -> None:
    finding = first_finding(scanned, FindingKind.MISSING_FIELDS, "Contact")
    with pytest.raises(PolicyViolation, match="not a writable field"):
        policy.check_proposal(finding, _update(finding, field, "a", "b"))


def test_rejects_changes_to_records_outside_the_finding(
    policy: WritePolicy, scanned: list[Finding]
) -> None:
    finding = first_finding(scanned, FindingKind.MISSING_FIELDS)
    with pytest.raises(PolicyViolation, match="outside the finding"):
        policy.check_proposal(
            finding, _update(finding, "Industry", None, "Tech", record_id="001OTHER")
        )


def test_rejects_noop_changes(policy: WritePolicy, scanned: list[Finding]) -> None:
    finding = first_finding(scanned, FindingKind.MISSING_FIELDS)
    with pytest.raises(PolicyViolation, match="no-op"):
        policy.check_proposal(finding, _update(finding, "Industry", None, "  "))


def test_a_fix_must_itself_pass_format_validation(
    policy: WritePolicy, scanned: list[Finding]
) -> None:
    finding = next(
        f for f in scanned if f.kind is FindingKind.INVALID_FORMAT and f.details["field"] == "Phone"
    )
    bad = _update(finding, "Phone", finding.details["value"], "still not a phone")
    with pytest.raises(PolicyViolation, match="not a valid phone"):
        policy.check_proposal(finding, bad)


def test_rejects_non_string_and_oversized_values(
    policy: WritePolicy, scanned: list[Finding]
) -> None:
    finding = first_finding(scanned, FindingKind.MISSING_FIELDS)
    with pytest.raises(PolicyViolation, match="strings"):
        policy.check_proposal(finding, _update(finding, "Industry", None, 42))
    with pytest.raises(PolicyViolation, match="longer than"):
        policy.check_proposal(finding, _update(finding, "Industry", None, "x" * 300))


def test_merge_only_for_duplicates_and_only_within_the_cluster(
    policy: WritePolicy, scanned: list[Finding]
) -> None:
    dup = first_finding(scanned, FindingKind.DUPLICATE)
    policy.check_proposal(dup, merge_draft(dup))

    rogue = merge_draft(dup)
    assert rogue.merge is not None
    rogue.merge.victim_ids = ["001SOMEONEELSE"]
    with pytest.raises(PolicyViolation, match="outside the finding"):
        policy.check_proposal(dup, rogue)

    stale = first_finding(scanned, FindingKind.STALE)
    wrong_kind = merge_draft(dup).model_copy(update={"finding_id": stale.id})
    with pytest.raises(PolicyViolation, match="only allowed for duplicate"):
        policy.check_proposal(stale, wrong_kind)


def test_proposal_must_answer_the_finding_it_is_checked_against(
    policy: WritePolicy, scanned: list[Finding]
) -> None:
    a, b = scanned[0], scanned[1]
    with pytest.raises(PolicyViolation, match="not"):
        policy.check_proposal(b, flag_draft(a))


def test_model_validation_rejects_malformed_proposals() -> None:
    with pytest.raises(ValueError, match="merge plan"):
        ProposalDraft(
            finding_id="f", action=ActionType.MERGE, confidence=Confidence.LOW, explanation="x"
        )
    with pytest.raises(ValueError, match="at least one change"):
        ProposalDraft(
            finding_id="f",
            action=ActionType.UPDATE_FIELD,
            confidence=Confidence.LOW,
            explanation="x",
        )
    with pytest.raises(ValueError, match="review note"):
        ProposalDraft(
            finding_id="f",
            action=ActionType.FLAG_FOR_REVIEW,
            confidence=Confidence.LOW,
            explanation="x",
        )
