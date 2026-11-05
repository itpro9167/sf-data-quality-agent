"""Run the agent over the seeded mock problems and score it against the labels."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date

from sfdq.agent import FixProposer, LLMClient, ProposeStats
from sfdq.eval.labels import LabelSet, finding_key, label_key, org_fingerprint
from sfdq.eval.metrics import Case, Metrics, compute_metrics, judge
from sfdq.models import Finding, FindingKind
from sfdq.policy import WritePolicy
from sfdq.rules import Rules
from sfdq.salesforce.mock import MockSalesforceClient
from sfdq.salesforce.mock_data import generate_mock_org
from sfdq.salesforce.readonly import ReadOnlySalesforceClient
from sfdq.scanners import run_scan


class EvalError(RuntimeError):
    """The eval cannot run (e.g. the labels do not belong to the generated org)."""


@dataclass
class EvalRun:
    """Everything one eval run produced."""

    provider: str
    model: str
    seed: int
    as_of: str
    records: dict[str, int]
    findings_total: int
    evaluated: int
    stats: ProposeStats
    duration_s: float
    cases: list[Case]
    metrics: Metrics
    subset: str = "all findings"
    label_description: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def is_mock(self) -> bool:
        """True when the canned mock LLM produced the proposals."""
        return self.provider == "mock"


def select_findings(
    findings: list[Finding], kinds: Iterable[FindingKind] | None, limit: int | None
) -> list[Finding]:
    """Filter by kind and, with ``limit``, take a deterministic round-robin sample across kinds."""
    wanted = set(kinds) if kinds is not None else set(FindingKind)
    pool = sorted((f for f in findings if f.kind in wanted), key=lambda f: (f.kind.value, f.id))
    if limit is None or limit >= len(pool):
        return pool
    buckets: dict[FindingKind, list[Finding]] = defaultdict(list)
    for f in pool:
        buckets[f.kind].append(f)
    chosen: list[Finding] = []
    while len(chosen) < limit and any(buckets.values()):
        for kind in list(buckets):
            if buckets[kind] and len(chosen) < limit:
                chosen.append(buckets[kind].pop(0))
    return chosen


def run_eval(
    *,
    rules: Rules,
    llm: LLMClient,
    labels: LabelSet,
    as_of: date | None = None,
    kinds: Iterable[FindingKind] | None = None,
    limit: int | None = None,
    on_progress: Callable[[int, int, Finding], None] | None = None,
) -> EvalRun:
    """Scan a freshly generated mock org, run the agent on its findings and score the proposals.

    Nothing is persisted: no queue database, no mock-org snapshot, no Salesforce writes (the agent
    only ever sees a read-only client).

    Raises:
        EvalError: if the labels reference records that the generated org does not contain.
    """
    started = time.monotonic()
    as_of = as_of or date.today()
    org = generate_mock_org(seed=labels.seed, as_of=as_of)
    sf = ReadOnlySalesforceClient(MockSalesforceClient(org))
    if labels.org_fingerprint and labels.org_fingerprint != org_fingerprint(org):
        raise EvalError(
            "these labels describe a different generated org (seed or generator changed); "
            "regenerate with `python -m sfdq.eval --write-labels`"
        )
    known = {r["Id"] for objs in ("Account", "Contact") for r in org.records[objs]}
    stray = {i for fix in labels.fixes for i in fix.record_ids} - known
    if stray:
        raise EvalError(
            f"labels reference {len(stray)} records that are not in the generated org "
            f"(seed {labels.seed}); regenerate with `python -m sfdq.eval --write-labels`"
        )

    report = run_scan(sf, rules, as_of=as_of)
    findings = report.findings
    by_key = {finding_key(f): f for f in findings}
    chosen = select_findings(findings, kinds, limit)
    chosen_ids = {f.id for f in chosen}
    proposer = FixProposer(llm, sf, rules, WritePolicy(rules))
    stats = ProposeStats()
    drafts = {}
    for i, f in enumerate(chosen, 1):
        drafts[f.id] = proposer.propose(f, stats)
        if on_progress:
            on_progress(i, len(chosen), f)

    wanted = set(kinds) if kinds is not None else set(FindingKind)
    cases: list[Case] = []
    matched: set[str] = set()
    for fix in labels.fixes:
        if fix.issue_type not in wanted:
            continue
        finding = by_key.get(label_key(fix))
        case = Case(label=fix, finding=finding)
        if finding is not None:
            matched.add(finding.id)
            draft = drafts.get(finding.id)
            if draft is not None:
                case.draft = draft
                case.action_ok, case.correct, case.reason = judge(fix, draft)
        else:
            case.reason = "NOT DETECTED by the scanners"
        cases.append(case)
    for f in findings:  # findings that match no seeded problem
        if f.kind in wanted and f.id not in matched:
            case = Case(label=None, finding=f, draft=drafts.get(f.id))
            case.reason = "finding does not match any seeded problem (false positive)"
            cases.append(case)

    reported_dups = [frozenset(f.record_ids) for f in findings if f.kind is FindingKind.DUPLICATE]
    flagged = sum(
        any(set(c.record_ids) <= d for d in reported_dups) for c in labels.negative_controls
    )
    notes = []
    if limit is not None or kinds is not None:
        notes.append("Partial run: agent metrics cover only the selected findings.")
    return EvalRun(
        provider=llm.name,
        model=llm.model,
        seed=labels.seed,
        as_of=as_of.isoformat(),
        records={k: len(org.records[k]) for k in ("Account", "Contact")},
        findings_total=len(findings),
        evaluated=len(chosen_ids),
        stats=stats,
        duration_s=time.monotonic() - started,
        cases=cases,
        metrics=compute_metrics(cases, flagged, len(labels.negative_controls)),
        subset="all findings"
        if limit is None and kinds is None
        else f"{len(chosen_ids)} selected findings",
        label_description=labels.description,
        notes=notes,
    )
