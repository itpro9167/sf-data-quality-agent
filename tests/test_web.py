"""Dashboard: rendering, filters, the approve/reject/undo flow, CSRF guard and XSS safety."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sfdq.models import Finding, FindingKind, Status
from sfdq.queue import QueueStore
from sfdq.settings import Settings
from sfdq.web.app import create_app
from tests.conftest import first_finding, flag_draft, format_fix_draft, merge_draft

HX = {"HX-Request": "true"}


@pytest.fixture
def client(store: QueueStore, settings: Settings, scanned: list[Finding]) -> TestClient:
    """12 Account-merge proposals + 1 stale flag + 1 format fix = 14 pending proposals."""
    for f in scanned:
        if f.kind is FindingKind.DUPLICATE and f.object_type == "Account":
            store.add_proposal(merge_draft(f), "agent")
    store.add_proposal(flag_draft(first_finding(scanned, FindingKind.STALE)), "agent")
    fix = next(
        f for f in scanned if f.kind is FindingKind.INVALID_FORMAT and f.details["suggestion"]
    )
    store.add_proposal(format_fix_draft(fix), "agent")
    return TestClient(create_app(settings, store))


def cards(html: str) -> int:
    return html.count('<article class="card ')


def test_queue_page_lists_pending_cards_with_counters(client: TestClient) -> None:
    html = client.get("/").text
    assert cards(html) == 14
    assert "Pending review" in html and "Keep" in html and "Merge away" in html
    assert "Creates a Task for the record owner" in html
    assert "<del>" in html and "<ins>" in html  # the before/after diff for the field fix


def test_security_headers(client: TestClient) -> None:
    headers = client.get("/").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "unsafe-inline" not in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"


def test_state_changes_require_the_htmx_header(client: TestClient, store: QueueStore) -> None:
    assert client.post("/proposals/1/approve").status_code == 403  # forged cross-site form
    assert store.get_proposal(1).status is Status.PENDING


def test_approve_updates_queue_audit_log_and_counters(
    client: TestClient, store: QueueStore
) -> None:
    response = client.post("/proposals/1/approve", headers=HX, data={"note": "same company"})
    assert response.status_code == 200
    assert "state-approved" in response.text and 'hx-swap-oob="true"' in response.text
    item = store.get_proposal(1)
    assert item.status is Status.APPROVED and item.decided_by == "web:tester"
    assert item.decision_note == "same company"
    assert store.audit_entries()[0].event == "proposal_approved"
    assert not item.applied  # approving never touches Salesforce


def test_reject_then_undo(client: TestClient, store: QueueStore) -> None:
    assert client.post("/proposals/2/reject", headers=HX).status_code == 200
    assert store.get_proposal(2).status is Status.REJECTED
    assert client.post("/proposals/2/reopen", headers=HX).status_code == 200
    assert store.get_proposal(2).status is Status.PENDING


def test_double_decision_returns_409_with_the_current_card(client: TestClient) -> None:
    client.post("/proposals/1/approve", headers=HX)
    response = client.post("/proposals/1/reject", headers=HX)
    assert response.status_code == 409 and "only pending proposals" in response.text


def test_unknown_proposal_is_404(client: TestClient) -> None:
    assert client.post("/proposals/999/approve", headers=HX).status_code == 404


def test_filters_tabs_and_pagination(client: TestClient) -> None:
    client.post("/proposals/1/approve", headers=HX)
    assert cards(client.get("/?status=approved").text) == 1
    assert cards(client.get("/?status=pending").text) == 13
    assert cards(client.get("/?status=all&kind=stale").text) == 1
    assert cards(client.get("/?status=all&kind=duplicate&confidence=high").text) >= 1
    assert "No proposals match" in client.get("/?status=rejected").text
    assert client.get("/?status=bogus&kind=nonsense&page=99").status_code == 200
    partial = client.get("/?status=all", headers={"HX-Request": "true", "HX-Target": "queue"}).text
    assert "<html" not in partial and cards(partial) == 14


def test_record_text_is_html_escaped(
    store: QueueStore, settings: Settings, scanned: list[Finding]
) -> None:
    finding = first_finding(scanned, FindingKind.STALE)
    evil = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    poisoned = finding.model_copy(update={"summary": f"Account '{evil}' is stale"})
    store.record_scan([poisoned], "test")
    draft = flag_draft(poisoned, note=evil).model_copy(update={"explanation": evil})
    store.add_proposal(draft, "agent")
    html = TestClient(create_app(settings, store)).get("/?status=all").text
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)" in html


def test_audit_page_shows_chain_status_and_events(client: TestClient) -> None:
    client.post("/proposals/1/approve", headers=HX)
    html = client.get("/audit").text
    assert "Hash chain verified" in html and "proposal_approved" in html
    assert "proposal #1" in client.get("/audit?proposal=1").text


def test_healthz_and_static(client: TestClient) -> None:
    assert client.get("/healthz").json()["queue"]["pending"] == 14
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/docs").status_code == 404  # no auto-generated API docs exposed
