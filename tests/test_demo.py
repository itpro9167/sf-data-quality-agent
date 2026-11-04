"""The deployable demo: seeding, public-demo mode, the container entrypoint and deploy files."""

from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from sfdq.cli import app
from sfdq.demo import seed_demo
from sfdq.models import Status
from sfdq.queue import QueueStore
from sfdq.rules import load_rules
from sfdq.settings import ConfigError, load_settings
from sfdq.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
HX = {"HX-Request": "true"}
runner = CliRunner()


def demo_settings(tmp_path: Path, **extra: str):
    return load_settings(
        {"SFDQ_HOME": str(tmp_path / "data"), "SFDQ_MODE": "mock", "SFDQ_PUBLIC_DEMO": "1", **extra}
    )


# ---- seeding --------------------------------------------------------------------------------
def test_seed_demo_builds_the_org_and_a_full_pending_queue(tmp_path: Path) -> None:
    settings = demo_settings(tmp_path)
    summary = seed_demo(settings, load_rules())
    assert (summary.seeded, summary.findings, summary.pending) == (True, 232, 232)
    store = QueueStore(settings.db_path)
    assert store.counts()["pending"] == 232 and settings.mock_org_path.exists()
    assert {p.source for p in store.list_proposals()} == {"mock"}
    assert store.verify_audit_chain().ok


def test_seed_demo_is_idempotent_and_force_starts_over(tmp_path: Path) -> None:
    settings, rules = demo_settings(tmp_path), load_rules()
    seed_demo(settings, rules)
    store = QueueStore(settings.db_path)
    store.decide(1, Status.APPROVED, "visitor")
    audit_len = len(store.audit_entries(limit=1000))

    again = seed_demo(settings, rules)
    assert again.seeded is False and again.pending == 231  # a visitor's decision is preserved
    assert len(store.audit_entries(limit=1000)) == audit_len

    reset = seed_demo(settings, rules, force=True)
    assert reset.seeded is True and reset.pending == 232
    fresh = QueueStore(settings.db_path)
    assert fresh.counts()["approved"] == 0 and fresh.verify_audit_chain().ok


def test_seed_demo_never_uses_a_paid_llm_and_never_touches_a_real_org(tmp_path: Path) -> None:
    with_key = load_settings({"SFDQ_HOME": str(tmp_path / "a"), "ANTHROPIC_API_KEY": "sk-test"})
    assert with_key.llm_mode == "real"
    seed_demo(with_key, load_rules())  # would raise if it tried to call the API with a fake key
    assert {p.source for p in QueueStore(with_key.db_path).list_proposals()} == {"mock"}

    real_org = load_settings(
        {
            "SFDQ_HOME": str(tmp_path / "b"),
            "SF_USERNAME": "u",
            "SF_PASSWORD": "p",
            "SF_SECURITY_TOKEN": "t",
        }
    )
    with pytest.raises(ConfigError, match="mock org"):
        seed_demo(real_org, load_rules())
    assert not (tmp_path / "b").exists()


# ---- public-demo mode -----------------------------------------------------------------------
def test_public_demo_refuses_real_credentials() -> None:
    creds = {"SF_USERNAME": "u", "SF_PASSWORD": "p", "SF_SECURITY_TOKEN": "t"}
    for env in ({**creds}, {"ANTHROPIC_API_KEY": "sk-x"}):
        with pytest.raises(ConfigError, match="only runs on mock data"):
            load_settings({**env, "SFDQ_PUBLIC_DEMO": "1"})
    forced = load_settings({**creds, "SFDQ_PUBLIC_DEMO": "true", "SFDQ_MODE": "mock"})
    assert forced.public_demo and forced.salesforce_mode == "mock"
    assert (
        load_settings({}).public_demo is False
        and load_settings({"SFDQ_PUBLIC_DEMO": "0"}).public_demo is False
    )


def test_dashboard_shows_the_demo_banner_only_in_demo_mode(tmp_path: Path) -> None:
    demo = demo_settings(tmp_path)
    seed_demo(demo, load_rules())
    html = TestClient(create_app(demo)).get("/").text
    assert "Public demo" in html and "Reset demo data" in html

    normal = load_settings({"SFDQ_HOME": str(tmp_path / "data"), "SFDQ_MODE": "mock"})
    plain = TestClient(create_app(normal))
    assert "Public demo" not in plain.get("/").text
    assert plain.post("/demo/reset", headers=HX).status_code == 404


def test_reset_button_restores_the_original_queue(tmp_path: Path) -> None:
    settings = demo_settings(tmp_path)
    seed_demo(settings, load_rules())
    client = TestClient(create_app(settings))
    for pid in (1, 2, 3):
        assert client.post(f"/proposals/{pid}/approve", headers=HX).status_code == 200
    assert client.get("/healthz").json()["queue"]["pending"] == 229

    assert client.post("/demo/reset").status_code == 403  # same CSRF guard as every state change
    response = client.post("/demo/reset", headers=HX)
    assert response.status_code == 204 and response.headers["hx-redirect"] == "/"
    assert client.get("/healthz").json()["queue"] == {
        "pending": 232, "approved": 0, "rejected": 0, "applied": 0, "awaiting_apply": 0,
    }  # fmt: skip
    assert "Pending review" in client.get("/").text  # the app keeps working after the rebuild


# ---- CLI ------------------------------------------------------------------------------------
def test_cli_seed_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for var in ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SFDQ_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SFDQ_MODE", "mock")
    first = runner.invoke(app, ["seed-demo"])
    assert first.exit_code == 0 and "Demo seeded: 232 findings, 232 pending" in first.output
    second = runner.invoke(app, ["seed-demo"])
    assert "already present" in " ".join(second.output.split())
    assert "Demo seeded" in runner.invoke(app, ["seed-demo", "--force"]).output

    monkeypatch.delenv("SFDQ_MODE")
    monkeypatch.setenv("SF_USERNAME", "u")
    monkeypatch.setenv("SF_PASSWORD", "p")
    monkeypatch.setenv("SF_SECURITY_TOKEN", "t")
    real = runner.invoke(app, ["seed-demo", "--force"])
    assert real.exit_code == 1 and "refusing to touch a real one" in " ".join(real.output.split())


# ---- container entrypoint (what `docker run` executes), without needing Docker --------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_entrypoint_script_seeds_then_serves(tmp_path: Path) -> None:
    script = ROOT / "docker" / "entrypoint.sh"
    assert subprocess.run(["sh", "-n", str(script)]).returncode == 0  # valid POSIX shell
    port = _free_port()
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "SF_USERNAME")},
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "SFDQ_HOME": str(tmp_path / "data"),
        "SFDQ_MODE": "mock",
        "SFDQ_PUBLIC_DEMO": "1",
        "PORT": str(port),
    }
    proc = subprocess.Popen(["sh", str(script)], cwd=tmp_path, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # fmt: skip
    try:
        body = ""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                body = (
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
                    .read()
                    .decode()
                )
                break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail(
                        f"entrypoint exited early:\n{proc.stdout.read() if proc.stdout else ''}"
                    )
                time.sleep(0.3)
        assert '"pending":232' in body, body  # sample data was preloaded before serving
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
        assert "Public demo" in page and 'class="card ' in page
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


# ---- deploy files ---------------------------------------------------------------------------
def test_dockerfile_is_a_slim_non_root_multistage_build_with_a_healthcheck() -> None:
    text = (ROOT / "Dockerfile").read_text()
    lines = [
        ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")
    ]
    first_from = next(i for i, ln in enumerate(lines) if ln.startswith("FROM"))
    assert any(ln.startswith("ARG PYTHON_IMAGE") for ln in lines[:first_from])
    assert sum(ln.startswith("FROM") for ln in lines) == 2 and "AS builder" in text
    assert "USER sfdq" in text and "10001" in text, "must not run as root"
    assert "HEALTHCHECK" in text and "/healthz" in text
    assert "SFDQ_MODE=mock" in text and "SFDQ_PUBLIC_DEMO=1" in text
    assert 'ENTRYPOINT ["entrypoint.sh"]' in text
    assert not re.search(r"(API_KEY|SECURITY_TOKEN|PASSWORD)", text), "no credentials in the image"
    for line in lines:  # every COPY source must exist in the repo
        if line.startswith("COPY") and "--from" not in line:
            *sources, _dest = shlex.split(line)[1:]
            for src in sources:
                assert (ROOT / src).exists(), f"Dockerfile copies missing path {src}"
    assert os.access(ROOT / "docker" / "entrypoint.sh", os.X_OK)


def test_dockerignore_keeps_secrets_and_local_state_out_of_the_image() -> None:
    entries = set((ROOT / ".dockerignore").read_text().split())
    assert {".env", ".git", ".venv", ".sfdq"} <= entries


def test_render_blueprint_matches_the_dockerfile_and_the_app(tmp_path: Path) -> None:
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    (service,) = blueprint["services"]
    assert service["type"] == "web" and service["runtime"] == "docker"
    assert (ROOT / service["dockerfilePath"]).exists() and service["plan"] == "free"
    env = {e["key"]: e["value"] for e in service["envVars"]}
    assert env == {"SFDQ_MODE": "mock", "SFDQ_PUBLIC_DEMO": "1"}
    assert "PORT" not in env, "Render injects $PORT; the entrypoint honours it"
    settings = demo_settings(tmp_path)
    seed_demo(settings, load_rules())
    assert TestClient(create_app(settings)).get(service["healthCheckPath"]).status_code == 200
