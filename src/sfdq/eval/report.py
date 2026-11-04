"""Markdown and JSON rendering of an :class:`EvalRun`."""

from __future__ import annotations

import json
from typing import Any

from sfdq.eval.metrics import AUTO_ACTIONS, Case, GroupMetrics
from sfdq.eval.runner import EvalRun

MAX_LISTED = 25
ISSUE_LABELS = {
    "duplicate": "Duplicates",
    "missing_fields": "Missing fields",
    "stale": "Stale records",
    "invalid_format": "Invalid formats",
}
DIFFICULTY_LABELS = {
    "mechanical": "Mechanical (fix follows from the bad value)",
    "needs_context": "Needs context (other fields / records)",
    "judgment": "Judgment (only the owner can decide)",
}


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _frac(num: int, den: int) -> str:
    return f"{num}/{den}"


def _row_issue(name: str, m: GroupMetrics) -> str:
    r = m.rates
    return (
        f"| {name} | {m.labeled} | {_pct(r['detection_precision'])} / {_pct(r['detection_recall'])} "
        f"| {_pct(r['auto_precision'])} ({_frac(m.auto_correct, m.auto_predicted)}) "
        f"| {_pct(r['auto_recall'])} ({_frac(m.auto_correct, m.auto_expected)}) "
        f"| {_pct(r['auto_f1'])} | {_pct(r['flag_accuracy'])} ({_frac(m.flag_correct, m.flag_expected)}) "
        f"| {m.unsafe_auto} |"
    )


def _problem_text(c: Case) -> str:
    where = c.finding.summary if c.finding else f"{c.label.id if c.label else '?'}"
    return where if len(where) <= 110 else where[:107] + "..."


def _got(c: Case) -> str:
    if c.draft is None:
        return "no proposal"
    if c.draft.action.value == "update_field":
        return "update " + ", ".join(f"{x.field}={x.after!r}" for x in c.draft.changes)
    if c.draft.merge:
        return f"merge, keep {c.draft.merge.survivor_id[-8:]}"
    return c.draft.action.value


def _expected(c: Case) -> str:
    if c.label is None:
        return "nothing (no such problem was seeded)"
    if c.label.expected_changes:
        return "update " + ", ".join(f"{k}={v!r}" for k, v in c.label.expected_changes.items())
    return c.label.expected_action.value


def _disagreements(run: EvalRun) -> list[Case]:
    bad: list[Case] = []
    for c in run.cases:
        if c.label is None or c.finding is None or (c.draft is not None and not c.correct):
            bad.append(c)

    def severity(c: Case) -> int:
        if (
            c.draft is not None
            and c.draft.action in AUTO_ACTIONS
            and not (c.label and c.label.auto_fix)
        ):
            return 0  # unsafe auto-fix: most serious
        if c.draft is not None and c.draft.action in AUTO_ACTIONS and not c.correct:
            return 1  # wrong auto-fix
        return 2  # missed opportunity / detection gap

    return sorted(bad, key=severity)


def render_markdown(run: EvalRun) -> str:
    """Render the full report."""
    m = run.metrics
    o = m.overall.rates
    agent = f"`{run.provider}` (`{run.model}`)"
    lines = [
        "# sfdq eval report",
        "",
        f"- **Agent:** {agent}"
        + (
            " (canned deterministic baseline; set `ANTHROPIC_API_KEY` to evaluate Claude)"
            if run.is_mock
            else ""
        ),
        f"- **Dataset:** seeded mock org, seed {run.seed}, as of {run.as_of}: "
        f"{run.records['Account']} Accounts, {run.records['Contact']} Contacts, "
        f"{m.overall.labeled} labeled problems, {m.decoys_total} look-alike decoys",
        f"- **Evaluated:** {run.subset} ({run.evaluated} of {run.findings_total} scanner findings)",
        f"- **Cost:** {run.stats.llm_calls} model calls, {run.stats.input_tokens:,} input / "
        f"{run.stats.output_tokens:,} output tokens"
        + (" (simulated)" if run.is_mock else "")
        + f", {run.duration_s:.1f}s",
    ]
    lines += [f"- **Note:** {n}" for n in run.notes]
    lines += [
        "",
        "## Headline",
        "",
        f"- **Auto-fix precision {_pct(o['auto_precision'])}**: {m.overall.auto_correct} of "
        f"{m.overall.auto_predicted} automated proposals (merge / update) were exactly right. "
        "A wrong auto-fix corrupts data, so this is the number to protect.",
        f"- **Auto-fix recall {_pct(o['auto_recall'])}**: it found the right automated fix for "
        f"{m.overall.auto_correct} of {m.overall.auto_expected} problems that have one.",
        f"- **Restraint {_pct(o['flag_accuracy'])}**: it asked the owner on "
        f"{m.overall.flag_correct} of {m.overall.flag_expected} problems that need a human, "
        f"with {m.overall.unsafe_auto} unsafe auto-fixes.",
        f"- **Detection** (deterministic scanners): precision {_pct(o['detection_precision'])}, "
        f"recall {_pct(o['detection_recall'])}; {m.decoys_flagged} of {m.decoys_total} look-alike "
        "decoys wrongly reported as duplicates.",
        "",
        "## By issue type",
        "",
        "| Issue type | Seeded | Detection P / R | Auto-fix precision | Auto-fix recall | Auto-fix F1 | Flag accuracy | Unsafe auto-fixes |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [_row_issue(ISSUE_LABELS[k], g) for k, g in m.by_issue_type.items()]
    lines.append(_row_issue("**All**", m.overall))
    lines += [
        "",
        "## By difficulty",
        "",
        "Recall of the *automated* fix, split by how much the problem requires of the agent.",
        "",
        "| Tier | Problems | Correct | Recall |",
        "|---|---:|---:|---:|",
    ]
    for k, g in m.by_difficulty.items():
        if k == "judgment":
            lines.append(
                f"| {DIFFICULTY_LABELS[k]} | {g.labeled} | {g.flag_correct} flagged "
                f"| {_pct(g.rates['flag_accuracy'])} |"
            )
        else:
            lines.append(
                f"| {DIFFICULTY_LABELS[k]} | {g.auto_expected} | {g.auto_correct} "
                f"| {_pct(g.rates['auto_recall'])} |"
            )
    bad = _disagreements(run)
    lines += ["", f"## Where the agent and the labels disagree ({len(bad)})", ""]
    if not bad:
        lines.append("Nothing: every evaluated proposal matched its label.")
    else:
        lines += [
            "Most serious first (unsafe auto-fix, then wrong auto-fix, then missed fixes and detection gaps).",
            "",
            "| Problem | Expected | Got | Why |",
            "|---|---|---|---|",
        ]
        for c in bad[:MAX_LISTED]:
            lines.append(
                f"| {_problem_text(c).replace('|', '/')} | {_expected(c).replace('|', '/')} "
                f"| {_got(c).replace('|', '/')} | {c.reason.replace('|', '/')} |"
            )
        if len(bad) > MAX_LISTED:
            lines.append(
                f"\n...and {len(bad) - MAX_LISTED} more (see the JSON output for all of them)."
            )
    lines += [
        "",
        "## Method",
        "",
        "- The seeded mock org is generated deterministically and every corrupted value is recorded with its "
        "*correct* value. Labels (`src/sfdq/eval/data/expected_fixes.json`) say what a careful analyst would do "
        "for each seeded problem, derived from that ground truth rather than from any scanner or agent output.",
        "- The scanners run first; the agent then proposes a fix for each finding through the same tool-use loop "
        "and write policy as production, against a read-only Salesforce client. Nothing is persisted.",
        "- A proposal is **correct** when its action matches the label and the details match: the right "
        "survivor and record set for a merge, the correct value (websites compared by domain) for an update, or "
        "a flag where only the owner can decide.",
        "- **Precision** = correct automated proposals / all automated proposals. **Recall** = correct automated "
        "proposals / problems that have an automated fix. **Unsafe auto-fix** = an automated proposal where "
        "the label says to ask the owner.",
        "- Re-generate the labels with `python -m sfdq.eval --write-labels`; a test fails if they drift from the generator.",
        "",
    ]
    return "\n".join(lines)


def _group_dict(g: GroupMetrics) -> dict[str, Any]:
    data = {k: v for k, v in g.__dict__.items() if k != "rates"}
    return {**data, **g.rates}


def render_json(run: EvalRun) -> str:
    """Machine-readable results (for dashboards, regression gates, diffing two runs)."""
    m = run.metrics
    payload = {
        "agent": {"provider": run.provider, "model": run.model, "mock": run.is_mock},
        "dataset": {"seed": run.seed, "as_of": run.as_of, "records": run.records},
        "evaluated": run.evaluated,
        "findings_total": run.findings_total,
        "usage": {
            "llm_calls": run.stats.llm_calls,
            "input_tokens": run.stats.input_tokens,
            "output_tokens": run.stats.output_tokens,
            "duration_s": round(run.duration_s, 2),
        },
        "overall": _group_dict(m.overall),
        "by_issue_type": {k: _group_dict(g) for k, g in m.by_issue_type.items()},
        "by_difficulty": {k: _group_dict(g) for k, g in m.by_difficulty.items()},
        "decoys": {"flagged_as_duplicate": m.decoys_flagged, "total": m.decoys_total},
        "disagreements": [
            {
                "label_id": c.label.id if c.label else None,
                "finding_id": c.finding.id if c.finding else None,
                "problem": _problem_text(c),
                "expected": _expected(c),
                "got": _got(c),
                "reason": c.reason,
            }
            for c in _disagreements(run)
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
