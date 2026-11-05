"""MCP server: tool surface, the review workflow, and the guarantee that it can never apply."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

import sfdq.mcp_server as mcp_module
from sfdq.mcp_server import TOOL_NAMES, build_server
from sfdq.models import Status
from sfdq.queue import QueueStore
from sfdq.rules import load_rules
from sfdq.runtime import Runtime
from sfdq.salesforce.base import SalesforceClient
from sfdq.salesforce.mock import MockSalesforceClient
from sfdq.salesforce.readonly import ReadOnlyError, ReadOnlySalesforceClient
from sfdq.settings import Settings


@pytest.fixture
def rt(settings: Settings) -> Runtime:
    return Runtime(settings, load_rules(), QueueStore(settings.db_path))


class Driver:
    """Calls MCP tools through a real in-process MCP client session."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.server = build_server(rt)

    def call(self, tool: str, **args: Any) -> Any:
        async def go() -> Any:
            async with Client(self.server) as client:
                return await client.call_tool(tool, args)

        return asyncio.run(go())

    def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        result = self.call(tool, **args)
        assert not result.is_error, result.content
        assert result.structured_content is not None
        return dict(result.structured_content)

    def err(self, tool: str, **args: Any) -> str:
        result = self.call(tool, **args)
        assert result.is_error, "expected a tool error"
        return str(result.content[0].text)  # type: ignore[union-attr]


@pytest.fixture
def d(rt: Runtime) -> Driver:
    return Driver(rt)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- tool surface ---------------------------------------------------------------------------
def test_exposes_exactly_the_six_tools_and_no_apply(d: Driver) -> None:
    async def tools() -> Any:
        async with Client(d.server) as client:
            return (await client.list_tools()).tools

    listed = asyncio.run(tools())
    assert tuple(t.name for t in listed) == TOOL_NAMES
    assert not any("apply" in t.name or "write" in t.name for t in listed)
    by_name = {t.name: t for t in listed}
    for read_only in ("list_findings", "list_pending_fixes"):
        assert by_name[read_only].annotations.read_only_hint is True  # type: ignore[union-attr]
    for state_change in ("approve_fix", "reject_fix", "propose_fix", "run_scan"):
        ann = by_name[state_change].annotations
        assert ann.read_only_hint is False and ann.destructive_hint is False  # type: ignore[union-attr]
    assert "confirm" in by_name["approve_fix"].input_schema["required"]


def test_server_module_cannot_reach_the_apply_engine() -> None:
    source = inspect.getsource(mcp_module)
    assert "queue.applier" not in source and "Applier" not in source.split('"""', 2)[2]
    assert not hasattr(mcp_module, "Applier") and not hasattr(mcp_module, "apply_approved")


def test_read_only_client_allows_reads_and_refuses_every_write(
    mock_sf: MockSalesforceClient,
) -> None:
    ro = ReadOnlySalesforceClient(mock_sf)
    assert isinstance(ro, SalesforceClient) and "read-only" in ro.name
    account = ro.query_records("Account", limit=1)[0]
    assert ro.get_record("Account", account["Id"]) == account
    with pytest.raises(ReadOnlyError):
        ro.update_record("Account", account["Id"], {"Phone": "x"})
    with pytest.raises(ReadOnlyError):
        ro.merge_records("Account", account["Id"], ["001other"])
    with pytest.raises(ReadOnlyError):
        ro.create_record("Task", {"Subject": "x"})
    assert mock_sf.get_record("Account", account["Id"]) == account


# ---- the review workflow --------------------------------------------------------------------
def test_scan_then_list_findings_with_filters_and_paging(d: Driver) -> None:
    scan = d.ok("run_scan")
    assert scan["findings_total"] == 232 and scan["new"] == 232
    assert scan["by_issue_type"]["duplicate"] == {"Account": 12, "Contact": 18}
    assert d.ok("run_scan")["new"] == 0  # idempotent

    dups = d.ok("list_findings", issue_type="duplicate", object_type="Account", limit=5)
    assert dups["total_matching"] == 12 and dups["returned"] == 5
    first = dups["findings"][0]
    assert first["issue_type"] == "duplicate" and first["proposal_id"] is None
    assert first["duplicate_confidence"] >= 0.9  # best-scoring first
    page2 = d.ok("list_findings", issue_type="duplicate", object_type="Account", limit=5, offset=5)
    assert {f["finding_id"] for f in page2["findings"]}.isdisjoint(
        {f["finding_id"] for f in dups["findings"]}
    )
    assert d.ok("run_scan", issue_types=["stale"])["findings_total"] == 98


def test_propose_fix_queues_a_pending_proposal_and_is_idempotent(d: Driver) -> None:
    d.ok("run_scan")
    finding = d.ok("list_findings", issue_type="invalid_format", limit=1)["findings"][0]
    first = d.ok("propose_fix", finding_id=finding["finding_id"])
    assert first["already_proposed"] is False and first["status"] == "pending"
    assert first["explanation"] and first["confidence"] in ("high", "medium", "low")
    assert first["llm"]["provider"] == "mock" and first["llm"]["calls"] >= 2

    again = d.ok("propose_fix", finding_id=finding["finding_id"])
    assert again["already_proposed"] is True and again["proposal_id"] == first["proposal_id"]
    listed = d.ok("list_findings", proposal="exists")
    assert listed["total_matching"] == 1 and listed["findings"][0]["proposal_status"] == "pending"
    assert d.ok("list_findings", proposal="none")["total_matching"] == 231


def test_pending_fixes_show_before_after_and_filter(d: Driver) -> None:
    d.ok("run_scan")
    for issue in ("invalid_format", "duplicate"):
        for f in d.ok("list_findings", issue_type=issue, limit=2)["findings"]:
            d.ok("propose_fix", finding_id=f["finding_id"])
    pending = d.ok("list_pending_fixes")
    assert pending["total_pending"] == 4
    assert d.ok("list_pending_fixes", issue_type="duplicate")["total_pending"] == 2
    merge = next(f for f in pending["fixes"] if f["action"] == "merge")
    assert merge["merge"]["keep"] and merge["merge"]["merge_away"]
    update = next(f for f in pending["fixes"] if f["action"] == "update_field")
    assert {"record_id", "field", "before", "after"} <= update["changes"][0].keys()


def test_approve_requires_explicit_confirm_and_never_applies(d: Driver, rt: Runtime) -> None:
    d.ok("run_scan")
    org_file = rt.settings.mock_org_path
    finding = d.ok("list_findings", issue_type="duplicate", object_type="Account", limit=1)[
        "findings"
    ][0]
    pid = d.ok("propose_fix", finding_id=finding["finding_id"])["proposal_id"]
    org_before = sha(org_file)

    assert "confirm must be true" in d.err("approve_fix", proposal_id=pid, confirm=False)
    assert rt.store.get_proposal(pid).status is Status.PENDING

    approved = d.ok("approve_fix", proposal_id=pid, confirm=True, note="same company")
    assert approved["status"] == "approved" and approved["applied"] is False
    assert "NOT applied" in approved["next_step"] and "sfdq apply" in approved["next_step"]

    item = rt.store.get_proposal(pid)
    assert item.status is Status.APPROVED and item.decided_by == "mcp:tester"
    assert item.decision_note == "same company" and not item.applied
    assert rt.store.counts()["applied"] == 0 and rt.store.counts()["awaiting_apply"] == 1
    assert sha(org_file) == org_before, "approving over MCP must not touch Salesforce"
    assert rt.store.audit_entries()[0].event == "proposal_approved"
    assert rt.store.audit_entries()[0].actor == "mcp:tester"
    assert rt.store.verify_audit_chain().ok


def test_reject_and_double_decision_and_unknown_ids(d: Driver, rt: Runtime) -> None:
    d.ok("run_scan")
    ids = [
        d.ok("propose_fix", finding_id=f["finding_id"])["proposal_id"]
        for f in d.ok("list_findings", issue_type="stale", limit=2)["findings"]
    ]
    rejected = d.ok("reject_fix", proposal_id=ids[0], note="owner confirmed still active")
    assert rejected["status"] == "rejected"
    assert "only pending" in d.err("approve_fix", proposal_id=ids[0], confirm=True)
    assert "not found" in d.err("reject_fix", proposal_id=999)
    assert d.ok("list_pending_fixes")["total_pending"] == 1
    assert rt.store.get_proposal(ids[0]).decided_by == "mcp:tester"


def test_errors_are_explained_to_the_client_not_swallowed(d: Driver) -> None:
    d.ok("run_scan")
    assert "choose one of" in d.err("list_findings", issue_type="bogus")
    assert "Account" in d.err("list_findings", object_type="Opportunity")
    assert "list_findings" in d.err("propose_fix", finding_id="does-not-exist")
    assert "high, medium or low" in d.err("list_pending_fixes", confidence="certain")


def test_the_agent_inside_propose_fix_gets_a_read_only_salesforce(
    d: Driver, rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the agent layer tried to write, the wrapper would raise: prove the wiring."""
    seen: list[Any] = []
    original = mcp_module.FixProposer

    class Spy(original):  # type: ignore[valid-type, misc]
        def __init__(self, llm: Any, sf: Any, *a: Any, **k: Any) -> None:
            seen.append(sf)
            super().__init__(llm, sf, *a, **k)

    monkeypatch.setattr(mcp_module, "FixProposer", Spy)
    d.ok("run_scan")
    finding = d.ok("list_findings", limit=1)["findings"][0]
    d.ok("propose_fix", finding_id=finding["finding_id"])
    assert len(seen) == 1 and isinstance(seen[0], ReadOnlySalesforceClient)


# ---- real stdio transport -------------------------------------------------------------------
def test_stdio_transport_end_to_end(tmp_path: Path) -> None:
    """Spawn `sfdq mcp` as Claude Desktop would; any stray stdout print would break this."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sfdq.cli", "mcp"],
        env={"SFDQ_HOME": str(tmp_path / "home"), "SFDQ_MODE": "mock", "SFDQ_REVIEWER": "desk"},
        cwd=str(tmp_path),
    )

    async def session() -> tuple[list[str], dict[str, Any]]:
        async with Client(params) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            scan = await client.call_tool("run_scan", {"issue_types": ["stale"]})
            assert not scan.is_error
            return names, dict(scan.structured_content or {})

    names, scan = asyncio.run(asyncio.wait_for(session(), timeout=60))
    assert tuple(names) == TOOL_NAMES
    assert scan["findings_total"] == 98
