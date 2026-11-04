"""Eval harness: labels, judging, metric arithmetic, and that it can tell a bad agent from a good one."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sfdq.agent import MockLLMClient
from sfdq.agent.llm import LLMFatalError, LLMTurn, ToolCall
from sfdq.cli import app
from sfdq.eval import (
    build_labels,
    compute_metrics,
    load_labels,
    render_json,
    render_markdown,
    run_eval,
)
from sfdq.eval.labels import (
    ExpectedFix,
    LabelSet,
    _classify_format,
    default_labels_text,
    dump_labels,
    finding_key,
    label_key,
)
from sfdq.eval.metrics import Case, judge
from sfdq.eval.runner import EvalError, select_findings
from sfdq.models import ActionType, Confidence, FieldChange, FindingKind, MergePlan, ProposalDraft
from sfdq.rules import Rules
from sfdq.salesforce.mock import MockSalesforceClient
from sfdq.salesforce.mock_data import generate_mock_org
from sfdq.scanners import run_scan
from sfdq.scanners.formats import check_value
from tests.conftest import AS_OF

runner = CliRunner()


# ---- ground truth and labels ----------------------------------------------------------------
def test_generator_records_the_correct_value_for_every_corrupted_field() -> None:
    org = generate_mock_org(as_of=AS_OF)
    seeded = org.seeded
    rec = {r["Id"]: r for k in ("Account", "Contact") for r in org.records[k]}
    for items in seeded.invalid_formats.values():
        for rid, fld in items:
            truth = seeded.truth[f"{rid}:{fld}"]
            kind = "email" if "Email" in fld else "postal_code" if "Postal" in fld else "phone"
            country = rec[rid].get("BillingCountry") or rec[rid].get("MailingCountry")
            assert check_value(kind, truth, country).valid, f"truth for {rid}:{fld} must be valid"
            assert truth != rec[rid][fld]
    for gaps in seeded.missing_fields.values():
        for rid, fields in gaps.items():
            for fld in fields:
                assert seeded.truth[f"{rid}:{fld}"] not in (None, ""), "blanked values have truth"
                assert rec[rid][fld] in (None, "")


def test_bundled_labels_match_what_the_generator_produces() -> None:
    """Drift guard: if the generator changes, regenerate with `python -m sfdq.eval --write-labels`."""
    expected = dump_labels(build_labels(generate_mock_org(seed=42, as_of=AS_OF)))
    assert default_labels_text() == expected


def test_labels_and_scanner_findings_match_one_to_one(
    mock_sf: MockSalesforceClient, rules: Rules
) -> None:
    labels = load_labels()
    findings = run_scan(mock_sf, rules, as_of=AS_OF).findings
    assert len(labels.fixes) == len(findings) == 232
    assert {label_key(f) for f in labels.fixes} == {finding_key(f) for f in findings}
    assert len({f.id for f in labels.fixes}) == len(labels.fixes)


def test_label_mix_covers_every_issue_type_action_and_tier() -> None:
    labels = load_labels()
    mix = {(f.issue_type, f.expected_action, f.difficulty) for f in labels.fixes}
    assert (FindingKind.DUPLICATE, ActionType.MERGE, "needs_context") in mix
    assert (FindingKind.INVALID_FORMAT, ActionType.UPDATE_FIELD, "mechanical") in mix
    assert (FindingKind.INVALID_FORMAT, ActionType.UPDATE_FIELD, "needs_context") in mix
    assert (FindingKind.INVALID_FORMAT, ActionType.FLAG_FOR_REVIEW, "judgment") in mix
    assert (FindingKind.MISSING_FIELDS, ActionType.UPDATE_FIELD, "needs_context") in mix
    assert (FindingKind.STALE, ActionType.FLAG_FOR_REVIEW, "judgment") in mix
    assert all(
        f.expected_changes for f in labels.fixes if f.expected_action is ActionType.UPDATE_FIELD
    )
    assert all(
        f.acceptable_survivors for f in labels.fixes if f.expected_action is ActionType.MERGE
    )
    assert len(labels.negative_controls) == 10


@pytest.mark.parametrize(
    ("field", "corrupted", "truth", "record", "acct", "expected"),
    [
        ("Phone", " (415) 555-0132 ", "(415) 555-0132", {}, None, "mechanical"),
        ("Phone", "555-0132", "(415) 555-0132", {}, None, "judgment"),
        ("Phone", "N/A", "(415) 555-0132", {}, None, "judgment"),
        ("Email", "a.b@@x.com", "a.b@x.com", {}, "x.com", "mechanical"),
        ("Email", "a.b@gmial.com", "a.b@gmail.com", {}, "x.com", "mechanical"),
        ("Email", "a.b@x", "a.b@x.com", {}, "x.com", "needs_context"),
        ("Email", "a.b.x.com", "a.b@x.com", {}, "x.com", "needs_context"),
        ("Email", "a.b@x", "a.b@x.com", {}, None, "judgment"),  # no account domain to restore from
        ("BillingPostalCode", "33131-", "33131", {}, None, "mechanical"),
        ("MailingPostalCode", "m5v2t6", "M5V 2T6", {}, None, "mechanical"),
        ("BillingPostalCode", "2108", "02108", {"BillingState": "MA"}, None, "needs_context"),
        ("BillingPostalCode", "2108", "02108", {"BillingState": "CA"}, None, "judgment"),
        ("BillingPostalCode", "N/A", "33131", {}, None, "judgment"),
        ("BillingPostalCode", "94 105", "33131", {}, None, "judgment"),
    ],
)
def test_classify_format_difficulty(
    field: str, corrupted: str, truth: str, record: dict[str, Any], acct: str | None, expected: str
) -> None:
    assert _classify_format(field, corrupted, truth, record, acct) == expected


# ---- judging --------------------------------------------------------------------------------
def fix(**kw: Any) -> ExpectedFix:
    base: dict[str, Any] = {
        "id": "x", "issue_type": FindingKind.INVALID_FORMAT, "object_type": "Account",
        "record_ids": ["A1"], "expected_action": ActionType.UPDATE_FIELD, "difficulty": "mechanical",
        "field": "Phone", "expected_changes": {"Phone": "(415) 555-0132"}, "rationale": "r",
    }  # fmt: skip
    return ExpectedFix(**{**base, **kw})


def update(field: str, after: str | None, record: str = "A1") -> ProposalDraft:
    return ProposalDraft(
        finding_id="f", action=ActionType.UPDATE_FIELD, confidence=Confidence.HIGH, explanation="e",
        changes=[FieldChange(record_id=record, field=field, before="old", after=after)],
    )  # fmt: skip


def flag() -> ProposalDraft:
    return ProposalDraft(finding_id="f", action=ActionType.FLAG_FOR_REVIEW,
                         confidence=Confidence.LOW, explanation="e", review_note="ask")  # fmt: skip


def merge(survivor: str, victims: list[str]) -> ProposalDraft:
    return ProposalDraft(finding_id="f", action=ActionType.MERGE, confidence=Confidence.HIGH,
                         explanation="e", merge=MergePlan(survivor_id=survivor, victim_ids=victims))  # fmt: skip


def test_judge_update_field() -> None:
    label = fix()
    assert judge(label, update("Phone", "(415) 555-0132"))[1] is True
    ok, correct, why = judge(label, update("Phone", "(415) 555-9999"))
    assert ok and not correct and "correct value" in why
    assert judge(label, update("Phone", "(415) 555-0132", record="A2"))[1] is False
    assert judge(label, update("Industry", "Tech"))[1] is False  # right action, wrong field
    assert judge(label, flag()) == (
        False,
        False,
        "flag_for_review instead of update_field (fix was possible)",
    )


def test_judge_compares_websites_by_domain_not_string() -> None:
    label = fix(
        field=None, issue_type=FindingKind.MISSING_FIELDS, expected_changes={"Website": "acme.io"}
    )
    assert judge(label, update("Website", "https://www.Acme.io/"))[1] is True
    assert judge(label, update("Website", "https://acme.com"))[1] is False


def test_judge_flag_expected_treats_auto_fix_as_unsafe() -> None:
    label = fix(
        expected_action=ActionType.FLAG_FOR_REVIEW, expected_changes={}, difficulty="judgment"
    )
    assert judge(label, flag())[1] is True
    ok, correct, why = judge(label, update("Phone", "(212) 555-0100"))
    assert not ok and not correct and why.startswith("UNSAFE")


def test_judge_merge_checks_record_set_and_survivor_quality() -> None:
    label = fix(
        issue_type=FindingKind.DUPLICATE, field=None, record_ids=["A1", "A2", "A3"],
        expected_action=ActionType.MERGE, expected_changes={}, acceptable_survivors=["A1", "A2"],
    )  # fmt: skip
    assert judge(label, merge("A2", ["A1", "A3"]))[1] is True
    assert "less complete" in judge(label, merge("A3", ["A1", "A2"]))[2]
    assert "wrong set" in judge(label, merge("A1", ["A2"]))[2]


# ---- metric arithmetic ----------------------------------------------------------------------
def case(label: ExpectedFix | None, draft: ProposalDraft | None, *, detected: bool = True) -> Case:
    from sfdq.models import Finding

    finding = (
        Finding(
            id="f",
            kind=(label.issue_type if label else FindingKind.STALE),
            object_type="Account",
            record_ids=["A1"],
            summary="s",
        )
        if detected
        else None
    )
    c = Case(label=label, finding=finding, draft=draft)
    if label and draft:
        c.action_ok, c.correct, c.reason = judge(label, draft)
    return c


def test_precision_recall_math_on_hand_built_cases() -> None:
    good, wrong_value = fix(), fix()
    needs_human = fix(
        expected_action=ActionType.FLAG_FOR_REVIEW, expected_changes={}, difficulty="judgment"
    )
    cases = [
        case(good, update("Phone", "(415) 555-0132")),  # correct auto-fix
        case(wrong_value, update("Phone", "(415) 555-9999")),  # auto-fix, wrong value
        case(fix(), flag()),  # fixable but only flagged: a recall miss, not a precision error
        case(fix(), None, detected=False),  # scanner missed it: counts against recall
        case(needs_human, update("Phone", "(212) 555-0100")),  # UNSAFE auto-fix
        case(needs_human, flag()),  # correct restraint
        case(None, update("Phone", "(212) 555-0100")),  # finding that is not a real problem
    ]
    m = compute_metrics(cases).overall
    assert (m.auto_predicted, m.auto_correct, m.auto_expected) == (4, 1, 4)
    assert m.rates["auto_precision"] == pytest.approx(1 / 4)
    assert m.rates["auto_recall"] == pytest.approx(1 / 4)
    assert (m.flag_expected, m.flag_correct, m.unsafe_auto) == (2, 1, 1)
    assert (m.labeled, m.detected, m.false_positives) == (6, 5, 1)
    assert m.rates["detection_recall"] == pytest.approx(5 / 6)
    assert m.rates["detection_precision"] == pytest.approx(5 / 6)


def test_undefined_rates_are_none_not_zero_division() -> None:
    only_flags = [
        case(
            fix(
                expected_action=ActionType.FLAG_FOR_REVIEW,
                expected_changes={},
                difficulty="judgment",
            ),
            flag(),
        )
    ]
    rates = compute_metrics(only_flags).overall.rates
    assert (
        rates["auto_precision"] is None
        and rates["auto_recall"] is None
        and rates["auto_f1"] is None
    )
    assert rates["flag_accuracy"] == 1.0


def test_partial_runs_do_not_penalise_unevaluated_labels() -> None:
    skipped = case(fix(), None)  # detected, but the agent never ran on it (e.g. --limit)
    m = compute_metrics([skipped, case(fix(), update("Phone", "(415) 555-0132"))]).overall
    assert m.auto_expected == 1 and m.rates["auto_recall"] == 1.0


# ---- running it -----------------------------------------------------------------------------
def test_select_findings_filters_and_samples_round_robin(
    mock_sf: MockSalesforceClient, rules: Rules
) -> None:
    findings = run_scan(mock_sf, rules, as_of=AS_OF).findings
    sample = select_findings(findings, None, 8)
    assert len(sample) == 8 and {f.kind for f in sample} == set(FindingKind)
    assert [f.id for f in sample] == [
        f.id for f in select_findings(findings, None, 8)
    ]  # deterministic
    stale = select_findings(findings, [FindingKind.STALE], 5)
    assert len(stale) == 5 and {f.kind for f in stale} == {FindingKind.STALE}


def test_mock_agent_baseline_regression_gate(rules: Rules) -> None:
    run = run_eval(rules=rules, llm=MockLLMClient(), labels=load_labels(), as_of=AS_OF)
    o = run.metrics.overall
    assert run.is_mock and run.evaluated == run.findings_total == 232
    assert o.rates["detection_precision"] == 1.0 and o.rates["detection_recall"] == 1.0
    assert run.metrics.decoys_flagged == 0 and run.metrics.decoys_total == 10
    assert o.rates["auto_precision"] == 1.0 and o.unsafe_auto == 0
    assert o.rates["auto_recall"] >= 0.9 and o.rates["flag_accuracy"] == 1.0
    # the three known gaps are context-dependent email repairs the canned agent does not attempt
    misses = [c for c in run.cases if c.label and c.draft and not c.correct]
    assert {c.label.field for c in misses if c.label} == {"Email"} and len(misses) == 3
    assert run.metrics.by_difficulty["mechanical"].rates["auto_recall"] == 1.0


class ScriptedAgent:
    """An LLM that answers every finding with a fixed strategy (to test the harness itself)."""

    name, model = "scripted", "scripted-1"

    def __init__(self, strategy: str) -> None:
        self.strategy = strategy

    def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMTurn:
        f = json.loads(
            re.search(r"<finding>\n(.*?)\n</finding>", str(messages[0]["content"]), re.S).group(1)
        )  # type: ignore[union-attr]
        rid, kind = f["record_ids"][0], f["kind"]
        if self.strategy == "timid" or (kind == "stale" and self.strategy == "careful"):
            args: dict[str, Any] = {"action": "flag_for_review", "confidence": "low",
                                    "explanation": "Not sure.", "review_note": "Please check."}  # fmt: skip
        elif kind == "duplicate":  # reckless: merge, keeping the LAST record
            *victims, keep = f["record_ids"]
            args = {"action": "merge", "confidence": "high", "explanation": "Same.",
                    "survivor_id": keep, "victim_ids": victims}  # fmt: skip
        elif kind == "invalid_format":
            wrong = {"Phone": "(212) 555-0100", "MobilePhone": "(212) 555-0100", "Email": "wrong@example.com",
                     "BillingPostalCode": "99999", "MailingPostalCode": "99999"}[f["details"]["field"]]  # fmt: skip
            args = {"action": "update_field", "confidence": "high", "explanation": "Fix.",
                    "changes": [{"record_id": rid, "field": f["details"]["field"], "new_value": wrong}]}  # fmt: skip
        else:  # reckless on missing/stale: overwrite a writable field (an unsafe auto-fix)
            scribble = "Description" if f["object_type"] == "Account" else "Title"
            args = {"action": "update_field", "confidence": "high", "explanation": "Fill.",
                    "changes": [{"record_id": rid, "field": scribble, "new_value": "auto"}]}  # fmt: skip
        call = ToolCall("t1", "submit_proposal", args)
        return LLMTurn(content=[{"type": "tool_use", "id": "t1", "name": call.name, "input": args}],
                       tool_calls=[call], stop_reason="tool_use")  # fmt: skip


def test_harness_flags_a_reckless_agent(rules: Rules) -> None:
    run = run_eval(rules=rules, llm=ScriptedAgent("reckless"), labels=load_labels(), as_of=AS_OF)
    o = run.metrics.overall
    assert o.rates["auto_precision"] < 0.3, "wrong auto-fixes must tank precision"
    assert o.unsafe_auto > 100 and o.rates["flag_accuracy"] < 0.1
    md = render_markdown(run)
    assert "UNSAFE" in md and "Auto-fix precision" in md


def test_harness_flags_a_timid_agent_by_recall_not_precision(rules: Rules) -> None:
    run = run_eval(rules=rules, llm=ScriptedAgent("timid"), labels=load_labels(), as_of=AS_OF)
    o = run.metrics.overall
    assert o.rates["auto_precision"] is None and o.rates["auto_recall"] == 0.0
    assert o.unsafe_auto == 0 and o.rates["flag_accuracy"] == 1.0


def test_run_does_not_touch_any_state(
    rules: Rules, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_eval(rules=rules, llm=MockLLMClient(), labels=load_labels(), limit=10, as_of=AS_OF)
    assert list(tmp_path.iterdir()) == []


def test_eval_refuses_labels_built_for_a_different_dataset(rules: Rules) -> None:
    labels = load_labels()
    other_seed = LabelSet(
        seed=7, org_fingerprint=labels.org_fingerprint, description="x", fixes=labels.fixes
    )
    with pytest.raises(EvalError, match="different generated org"):
        run_eval(rules=rules, llm=MockLLMClient(), labels=other_seed, as_of=AS_OF)


def test_eval_refuses_labels_pointing_at_missing_records(rules: Rules) -> None:
    labels = load_labels()
    stray = labels.fixes[0].model_copy(update={"record_ids": ["001DOESNOTEXIST"], "id": "stray"})
    with pytest.raises(EvalError, match="not in the generated org"):
        run_eval(
            rules=rules,
            llm=MockLLMClient(),
            labels=LabelSet(
                seed=42, description="x", fixes=[stray]
            ),  # hand-written: no fingerprint
            as_of=AS_OF,
        )


# ---- reports --------------------------------------------------------------------------------
def test_markdown_and_json_reports(rules: Rules) -> None:
    run = run_eval(rules=rules, llm=MockLLMClient(), labels=load_labels(), as_of=AS_OF)
    md = render_markdown(run)
    for heading in ("# sfdq eval report", "## Headline", "## By issue type", "## By difficulty",
                    "## Where the agent and the labels disagree (3)", "## Method"):  # fmt: skip
        assert heading in md
    assert "| Invalid formats | 36 |" in md and "Auto-fix precision 100%" in md
    assert "canned deterministic baseline" in md
    data = json.loads(render_json(run))
    assert data["agent"]["mock"] is True and data["overall"]["auto_precision"] == 1.0
    assert set(data["by_issue_type"]) == {"duplicate", "missing_fields", "stale", "invalid_format"}
    assert len(data["disagreements"]) == 3 and data["decoys"] == {
        "flagged_as_duplicate": 0,
        "total": 10,
    }

    partial = run_eval(rules=rules, llm=MockLLMClient(), labels=load_labels(), limit=8, as_of=AS_OF)
    assert "Partial run" in render_markdown(partial)


# ---- CLI ------------------------------------------------------------------------------------
@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for var in (
        "SF_USERNAME",
        "SF_PASSWORD",
        "SF_SECURITY_TOKEN",
        "ANTHROPIC_API_KEY",
        "SFDQ_MODE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SFDQ_HOME", str(tmp_path / "sfdq"))
    return tmp_path


def test_cli_eval_writes_markdown_and_json_and_persists_nothing_else(cli_env: Path) -> None:
    result = runner.invoke(app, ["eval", "-o", "out/report.md", "--json", "out/results.json"])
    assert result.exit_code == 0, result.output
    assert "Eval agent: mock" in result.output and "Report written to" in result.output
    assert "# sfdq eval report" in (cli_env / "out" / "report.md").read_text()
    assert json.loads((cli_env / "out" / "results.json").read_text())["overall"]["labeled"] == 232
    assert not (cli_env / "sfdq").exists(), "eval must not create a queue DB or mock org snapshot"


def test_cli_eval_precision_gate(cli_env: Path) -> None:
    ok = runner.invoke(app, ["eval", "--limit", "20", "--min-auto-precision", "0.9"])
    assert ok.exit_code == 0, ok.output
    bad = runner.invoke(app, ["eval", "--limit", "20", "--min-auto-precision", "1.01"])
    assert bad.exit_code == 1 and "below the required" in " ".join(bad.output.split())
    vacuous = runner.invoke(app, ["eval", "--kind", "stale", "--min-auto-precision", "0.9"])
    assert vacuous.exit_code == 0 and "not applicable" in " ".join(vacuous.output.split())


def test_cli_eval_uses_claude_when_a_key_is_set_unless_mock_is_forced(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    built: list[str] = []

    def fake_build(settings: Any) -> ScriptedAgent:
        built.append(settings.model)
        agent = ScriptedAgent("timid")
        agent.name, agent.model = "claude", settings.model
        return agent

    monkeypatch.setattr("sfdq.cli.build_llm_client", fake_build)
    forced = runner.invoke(app, ["eval", "--mock", "--limit", "5"])
    assert forced.exit_code == 0 and "Eval agent: mock" in forced.output and built == []

    real = runner.invoke(app, ["eval", "--limit", "5"])  # small run: no cost prompt
    assert real.exit_code == 0 and "Eval agent: claude (claude-opus-5-5)" in real.output
    assert built == ["claude-opus-5-5"]

    declined = runner.invoke(app, ["eval"], input="n\n")  # full run with a paid model asks first
    assert declined.exit_code == 1 and "incurs API usage" in " ".join(declined.output.split())


def test_cli_eval_reports_an_unusable_llm_cleanly(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(ScriptedAgent):
        def complete(self, **_: Any) -> LLMTurn:
            raise LLMFatalError("invalid x-api-key")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-bad")
    monkeypatch.setattr("sfdq.cli.build_llm_client", lambda s: Broken("x"))
    result = runner.invoke(app, ["eval", "--limit", "3"])
    assert result.exit_code == 1 and "LLM unavailable" in " ".join(result.output.split())


def test_cli_eval_rejects_bad_labels_file(cli_env: Path) -> None:
    (cli_env / "bad.json").write_text("{not json")
    result = runner.invoke(app, ["eval", "--labels", "bad.json"])
    assert result.exit_code == 1 and "cannot start the eval" in " ".join(result.output.split())
