"""End-to-end: scan -> propose -> approve -> apply (dry run, then real) in mock mode.

Drives the real CLI (typer's CliRunner) and the real dashboard (FastAPI TestClient). No Salesforce
org and no API key are involved anywhere: this is exactly the "clone and run" experience.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from sfdq.cli import app
from sfdq.models import ActionType, FindingKind, Status
from sfdq.queue import QueueStore
from sfdq.settings import load_settings
from sfdq.web.app import create_app

HX = {"HX-Request": "true"}
runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)  # no stray .env can leak in
    for var in ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SFDQ_HOME", str(tmp_path / "sfdq"))
    monkeypatch.setenv("SFDQ_MODE", "mock")
    monkeypatch.setenv("SFDQ_REVIEWER", "alice")
    yield tmp_path / "sfdq"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args: str, input_: str | None = None, ok: bool = True) -> str:
    result = runner.invoke(app, list(args), input=input_)
    assert (result.exit_code == 0) == ok, result.output
    return result.output


def test_full_pipeline_scan_propose_approve_apply(home: Path) -> None:
    # 1. scan: deterministic, finds every seeded problem class
    out = run("scan")
    assert "Salesforce: mock | LLM: mock" in out
    assert "232 findings" in out and "232 new" in out
    store = QueueStore(home / "queue.db")
    assert sum(store.count_findings().values()) == 232

    # 2. propose: the (mock) agent queues a pending proposal for every finding
    out = run("propose")
    assert "Queued 232 proposals" in out and "0 used the safe fallback" in out
    assert store.counts()["pending"] == 232 and store.counts()["approved"] == 0
    assert "No findings need a proposal" in run("propose")  # idempotent

    # nothing is approved yet, so there is nothing to apply -- the human gate holds
    assert "Nothing to apply" in run("apply", "--dry-run")
    org_before = sha(home / "mock_org.json")

    # 3. a human approves one of each kind of fix in the dashboard (and rejects one)
    client = TestClient(create_app(load_settings(), store))
    pending = store.list_proposals(status=Status.PENDING)
    chosen = {}
    for p in pending:
        key = (p.finding.kind, p.action, p.finding.object_type)
        if (
            key
            in {
                (FindingKind.DUPLICATE, ActionType.MERGE, "Account"),
                (FindingKind.INVALID_FORMAT, ActionType.UPDATE_FIELD, "Account"),
                (FindingKind.STALE, ActionType.FLAG_FOR_REVIEW, "Contact"),
            }
            and key not in chosen
        ):
            chosen[key] = p
    assert len(chosen) == 3
    for p in chosen.values():
        assert client.post(f"/proposals/{p.id}/approve", headers=HX).status_code == 200
    rejected = next(p for p in pending if p.id not in {c.id for c in chosen.values()})
    assert client.post(f"/proposals/{rejected.id}/reject", headers=HX).status_code == 200
    assert store.counts()["approved"] == 3 and store.counts()["rejected"] == 1

    # 4. dry run: shows exactly what would happen, writes NOTHING, but IS audited
    out = run("apply", "--dry-run")
    assert "DRY RUN" in out and "3 planned (dry run)" in out
    assert "Merge Account" in out and "Update Account" in out
    assert sha(home / "mock_org.json") == org_before, "dry run must not modify the org"
    assert store.counts()["applied"] == 0
    assert [e.event for e in store.audit_entries(limit=3)] == ["apply_dry_run"] * 3

    # 5. real apply: asks for confirmation, then applies only the 3 approved fixes
    run("apply", input_="n\n", ok=False)  # declined -> aborted, nothing changes
    assert sha(home / "mock_org.json") == org_before
    out = run("apply", "--yes")
    assert "3 applied" in out
    assert sha(home / "mock_org.json") != org_before
    assert store.counts() == {
        "pending": 228,
        "approved": 3,
        "rejected": 1,
        "applied": 3,
        "awaiting_apply": 0,
    }
    assert "Nothing to apply" in run("apply", "--yes")  # applying twice is a no-op

    # 6. the org is actually better: re-scanning resolves the findings the fixes addressed
    run("scan")
    with sqlite3.connect(home / "queue.db") as conn:
        resolved = {
            row[0] for row in conn.execute("SELECT id FROM findings WHERE resolved_at IS NOT NULL")
        }
    merge_p = chosen[(FindingKind.DUPLICATE, ActionType.MERGE, "Account")]
    fix_p = chosen[(FindingKind.INVALID_FORMAT, ActionType.UPDATE_FIELD, "Account")]
    flag_p = chosen[(FindingKind.STALE, ActionType.FLAG_FOR_REVIEW, "Contact")]
    assert merge_p.finding_id in resolved and fix_p.finding_id in resolved
    assert flag_p.finding_id not in resolved, "a review Task does not make a stale record fresh"
    org = json.loads((home / "mock_org.json").read_text())
    (task,) = org["org"]["records"]["Task"]  # the owner-review Task from the flag proposal
    assert task["WhoId"] == flag_p.finding.record_ids[0]

    # 7. every step is in a tamper-evident audit log
    audit = run("audit", "-n", "200")
    assert "Hash chain verified" in audit
    events = [e.event for e in store.audit_entries(limit=500)]
    for expected in ("scan_completed", "proposal_created", "proposal_approved",
                     "proposal_rejected", "apply_dry_run", "apply_succeeded"):  # fmt: skip
        assert expected in events


def test_status_and_reset_commands(home: Path) -> None:
    run("scan")
    assert "Open findings: 232" in run("status")
    run("reset-demo", "--yes")
    assert "Open findings: 0" in run("status")


def test_reset_demo_refuses_to_touch_a_real_org(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SFDQ_MODE")
    for k, v in {"SF_USERNAME": "u", "SF_PASSWORD": "p", "SF_SECURITY_TOKEN": "t"}.items():
        monkeypatch.setenv(k, v)
    result = runner.invoke(app, ["reset-demo", "--yes"])
    assert result.exit_code == 1
    assert "refusing to touch a real Salesforce" in " ".join(result.output.split())


def test_bad_configuration_exits_cleanly(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SFDQ_MODE", "real")  # but no credentials
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 1 and "SFDQ_MODE=real but missing" in result.output
    monkeypatch.setenv("SFDQ_MODE", "mock")
    bad_rules = home.parent / "rules.yaml"
    bad_rules.write_text("stale: {months: 0}")
    result = runner.invoke(app, ["scan", "--rules", str(bad_rules)])
    assert result.exit_code == 1 and "invalid rules file" in result.output


def test_scan_kind_filter_and_custom_rules(home: Path) -> None:
    run("scan", "--kind", "stale")
    store = QueueStore(home / "queue.db")
    assert store.count_findings() == {"stale": 98}  # only the requested scanner ran

    # Tightening the staleness window via YAML alone changes the result (no code change).
    default_rules = resources.files("sfdq.config").joinpath("quality_rules.yaml").read_text("utf-8")
    custom = home.parent / "rules.yaml"
    custom.write_text(default_rules.replace("months: 12", "months: 6"))
    run("scan", "--kind", "stale", "--rules", str(custom))
    assert store.count_findings()["stale"] > 98
