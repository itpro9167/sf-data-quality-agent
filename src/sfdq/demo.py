"""Seed a ready-to-review demo: a fresh mock org plus a queue of pending proposals.

Used by the container entrypoint (``sfdq seed-demo``) so a visitor lands on a dashboard that
already has sample data, and by the dashboard's "Reset demo data" button in public-demo mode.
Always uses the canned mock LLM, so seeding is free, offline and identical every time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sfdq.agent import FixProposer, MockLLMClient, propose_all
from sfdq.policy import WritePolicy
from sfdq.queue import QueueStore
from sfdq.rules import Rules
from sfdq.salesforce.mock import MockSalesforceClient
from sfdq.scanners import run_scan
from sfdq.settings import ConfigError, Settings


@dataclass(frozen=True)
class DemoSummary:
    """What :func:`seed_demo` did."""

    seeded: bool  # False when sample data already existed and nothing was changed
    findings: int
    pending: int


def _wipe(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def seed_demo(settings: Settings, rules: Rules, *, force: bool = False) -> DemoSummary:
    """Create the sample org and review queue unless they already exist.

    Args:
        settings: Must be mock mode; a real Salesforce org is never touched or seeded.
        rules: Quality rules to scan with.
        force: Wipe any existing demo state first (mock org, queue and audit log).

    Raises:
        ConfigError: if Salesforce is in real mode.
    """
    if settings.salesforce_mode != "mock":
        raise ConfigError("seed-demo only works against the mock org; refusing to touch a real one")
    if not force and settings.db_path.exists():
        store = QueueStore(settings.db_path)
        counts = store.counts()
        findings = sum(store.count_findings().values())
        if findings:
            return DemoSummary(False, findings, counts["pending"])
    if force:
        _wipe(settings.db_path)
        _wipe(settings.mock_org_path)
    settings.home.mkdir(parents=True, exist_ok=True)
    sf = MockSalesforceClient.load_or_create(settings.mock_org_path)
    store = QueueStore(settings.db_path)
    report = run_scan(sf, rules, as_of=date.today())
    store.record_scan(report.findings, "demo-seed")
    llm = MockLLMClient()
    proposer = FixProposer(llm, sf, rules, WritePolicy(rules))
    propose_all(store.findings_without_proposal(), proposer, store, f"agent:{llm.name}")
    counts = store.counts()
    return DemoSummary(True, len(report.findings), counts["pending"])
