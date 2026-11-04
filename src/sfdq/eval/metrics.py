"""Judging proposals against labels, and the precision/recall arithmetic.

Definitions (all per group of cases: an issue type, a difficulty tier, or everything):

* **Detection** (the deterministic scanners): precision = findings that match a label / findings;
  recall = labels that were found / labels.
* **Auto-fix** (the agent): a *predicted* auto-fix is a ``merge`` or ``update_field`` proposal.
  It is *correct* when the label also expects an auto-fix and the details match (right survivor,
  right value). precision = correct / predicted; recall = correct / labels that expect an auto-fix.
  Precision is the one to watch: a wrong auto-fix corrupts data, a missed one just costs a click.
* **Flag accuracy** (restraint): among problems whose right answer is "ask the owner", the share
  where the agent did flag them. Its complement is the **unsafe auto-fix** count.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sfdq.eval.labels import ExpectedFix
from sfdq.models import ActionType, Finding, FindingKind, ProposalDraft
from sfdq.scanners.duplicates import website_domain

AUTO_ACTIONS = (ActionType.MERGE, ActionType.UPDATE_FIELD)


@dataclass
class Case:
    """One seeded problem and/or one scanner finding, with the agent's proposal if it ran."""

    label: ExpectedFix | None  # None: a finding that matches no seeded problem (false positive)
    finding: Finding | None  # None: a seeded problem the scanners missed
    draft: ProposalDraft | None = None  # None: the agent did not run on this finding
    action_ok: bool = False
    correct: bool = False
    reason: str = ""


def _same_value(field: str, expected: str, actual: object) -> bool:
    if actual is None:
        return False
    if field == "Website":
        return website_domain(str(actual)) == website_domain(expected)
    return str(actual).strip() == expected.strip()


def judge(label: ExpectedFix, draft: ProposalDraft) -> tuple[bool, bool, str]:
    """Return ``(action_ok, correct, reason)`` for one proposal against its label."""
    expected = label.expected_action
    action_ok = draft.action is expected
    if expected is ActionType.FLAG_FOR_REVIEW:
        if action_ok:
            return True, True, "flagged for the owner, as expected"
        return (
            False,
            False,
            (f"UNSAFE: proposed {draft.action.value} where only the owner can decide"),
        )
    if not action_ok:
        return False, False, f"{draft.action.value} instead of {expected.value} (fix was possible)"

    if expected is ActionType.MERGE:
        assert draft.merge is not None
        members = {draft.merge.survivor_id, *draft.merge.victim_ids}
        if members != set(label.record_ids):
            return True, False, "merged the wrong set of records"
        if draft.merge.survivor_id not in label.acceptable_survivors:
            return True, False, "kept a less complete record than another candidate"
        return True, True, "merged the right records, kept an acceptable survivor"

    changes = {c.field: c for c in draft.changes}
    if set(changes) != set(label.expected_changes):
        return (
            True,
            False,
            (f"changed {sorted(changes)} but the fix is {sorted(label.expected_changes)}"),
        )
    for fld, want in label.expected_changes.items():
        change = changes[fld]
        if change.record_id != label.record_ids[0]:
            return True, False, "edited the wrong record"
        if not _same_value(fld, want, change.after):
            return True, False, f"{fld}: proposed {change.after!r}, correct value is {want!r}"
    return True, True, "restored the correct value"


@dataclass
class GroupMetrics:
    """Counters and derived rates for one slice of the cases."""

    labeled: int = 0
    detected: int = 0
    false_positives: int = 0
    evaluated: int = 0
    auto_expected: int = 0
    auto_predicted: int = 0
    auto_correct: int = 0
    flag_expected: int = 0
    flag_correct: int = 0
    unsafe_auto: int = 0
    action_ok: int = 0
    correct: int = 0
    rates: dict[str, float | None] = field(default_factory=dict)

    def finalize(self) -> GroupMetrics:
        """Compute the derived rates (``None`` when the denominator is zero)."""

        def ratio(n: int, d: int) -> float | None:
            return n / d if d else None

        p, r = (
            ratio(self.auto_correct, self.auto_predicted),
            ratio(self.auto_correct, self.auto_expected),
        )
        f1 = 2 * p * r / (p + r) if p is not None and r is not None and (p + r) else None
        self.rates = {
            "detection_precision": ratio(self.detected, self.detected + self.false_positives),
            "detection_recall": ratio(self.detected, self.labeled),
            "auto_precision": p,
            "auto_recall": r,
            "auto_f1": f1,
            "flag_accuracy": ratio(self.flag_correct, self.flag_expected),
            "action_accuracy": ratio(self.action_ok, self.evaluated),
            "end_to_end_correct": ratio(self.correct, self.evaluated),
        }
        return self


def _tally(cases: list[Case]) -> GroupMetrics:
    m = GroupMetrics()
    for c in cases:
        lab, dec = c.label, c.draft
        if lab is not None:
            m.labeled += 1
            if c.finding is not None:
                m.detected += 1
        elif c.finding is not None:
            m.false_positives += 1
        predicted_auto = dec is not None and dec.action in AUTO_ACTIONS
        m.auto_predicted += predicted_auto
        if lab is None:
            continue
        in_scope = c.finding is None or dec is not None  # undetected counts against recall
        if in_scope and lab.auto_fix:
            m.auto_expected += 1
        if dec is None:
            continue
        m.evaluated += 1
        m.action_ok += c.action_ok
        m.correct += c.correct
        if lab.auto_fix:
            m.auto_correct += c.correct
        else:
            m.flag_expected += 1
            m.flag_correct += c.correct
            m.unsafe_auto += predicted_auto
    return m.finalize()


@dataclass
class Metrics:
    """All the numbers one eval run produces."""

    overall: GroupMetrics
    by_issue_type: dict[str, GroupMetrics]
    by_difficulty: dict[str, GroupMetrics]
    decoys_flagged: int = 0
    decoys_total: int = 0


def compute_metrics(cases: list[Case], decoys_flagged: int = 0, decoys_total: int = 0) -> Metrics:
    """Aggregate cases into overall / per-issue-type / per-difficulty metrics."""
    by_type: dict[str, list[Case]] = {}
    by_diff: dict[str, list[Case]] = {}
    for c in cases:
        kind = (c.label.issue_type if c.label else c.finding.kind).value  # type: ignore[union-attr]
        by_type.setdefault(kind, []).append(c)
        if c.label is not None:
            by_diff.setdefault(c.label.difficulty, []).append(c)
    order = [k.value for k in FindingKind]
    return Metrics(
        overall=_tally(cases),
        by_issue_type={k: _tally(by_type[k]) for k in order if k in by_type},
        by_difficulty={
            k: _tally(by_diff[k])
            for k in ("mechanical", "needs_context", "judgment")
            if k in by_diff
        },
        decoys_flagged=decoys_flagged,
        decoys_total=decoys_total,
    )
