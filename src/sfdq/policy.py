"""Write policy: the rules every proposal must satisfy before it can be queued or applied.

The agent is untrusted by design (it reads CRM free-text, which can contain prompt-injection, and
models make mistakes). So this policy is enforced twice: when a proposal is *created*, and again
when an approved proposal is *applied*. A proposal that fails here never reaches Salesforce.
"""

from __future__ import annotations

from sfdq.models import ActionType, Finding, FindingKind, ProposalDraft
from sfdq.rules import Rules
from sfdq.scanners.formats import check_value
from sfdq.util import is_blank

MAX_TEXT_LEN = 255
MAX_LONG_TEXT_LEN = 32_000
MAX_EXPLANATION_LEN = 2_000
_LONG_TEXT_FIELDS = frozenset({"Description"})


class PolicyViolation(ValueError):
    """Raised when a proposal breaks the write policy."""


class WritePolicy:
    """Validates proposals against the configured writable-field allow-list and format rules."""

    def __init__(self, rules: Rules) -> None:
        self._rules = rules

    def check_proposal(self, finding: Finding, proposal: ProposalDraft) -> None:
        """Raise :class:`PolicyViolation` unless ``proposal`` is a safe answer to ``finding``."""
        if proposal.finding_id != finding.id:
            raise PolicyViolation(
                f"proposal is for finding {proposal.finding_id}, not {finding.id}"
            )
        if len(proposal.explanation) > MAX_EXPLANATION_LEN:
            raise PolicyViolation(f"explanation exceeds {MAX_EXPLANATION_LEN} characters")
        scope = set(finding.record_ids)

        if proposal.action is ActionType.MERGE:
            if finding.kind is not FindingKind.DUPLICATE:
                raise PolicyViolation("merge is only allowed for duplicate findings")
            assert proposal.merge is not None  # guaranteed by ProposalDraft validation
            involved = {proposal.merge.survivor_id, *proposal.merge.victim_ids}
            if not involved <= scope:
                raise PolicyViolation(
                    f"merge references records outside the finding: {sorted(involved - scope)}"
                )
        if proposal.action is ActionType.FLAG_FOR_REVIEW and len(proposal.review_note or "") > 2000:
            raise PolicyViolation("review note exceeds 2000 characters")

        for change in proposal.changes:
            if change.record_id not in scope:
                raise PolicyViolation(f"change targets {change.record_id}, outside the finding")
            self.check_field_value(finding.object_type, change.field, change.after)
            if _same(change.before, change.after):
                raise PolicyViolation(f"{change.field} change is a no-op")

    def check_field_value(self, object_type: str, field: str, value: object) -> None:
        """Raise unless ``field`` is writable on ``object_type`` and ``value`` is acceptable."""
        if field not in self._rules.writable(object_type):
            raise PolicyViolation(
                f"{object_type}.{field} is not a writable field "
                f"(allowed: {sorted(self._rules.writable(object_type))})"
            )
        if value is None:
            return  # clearing a field is allowed for writable fields
        if not isinstance(value, str):
            raise PolicyViolation(f"{object_type}.{field}: values must be strings")
        limit = MAX_LONG_TEXT_LEN if field in _LONG_TEXT_FIELDS else MAX_TEXT_LEN
        if len(value) > limit:
            raise PolicyViolation(f"{object_type}.{field}: value longer than {limit} characters")
        for rule in self._rules.invalid_formats.get(object_type, []):
            if rule.field == field and not is_blank(value):
                result = check_value(rule.type, value, None)
                if not result.valid:
                    raise PolicyViolation(
                        f"proposed {field} {value!r} is not a valid {rule.type}: {result.problem}"
                    )


def _same(a: object, b: object) -> bool:
    """Equality that treats ``None`` and whitespace-only strings as the same 'blank'."""
    if is_blank(a) and is_blank(b):
        return True
    return a == b
