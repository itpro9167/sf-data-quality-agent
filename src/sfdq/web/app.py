"""FastAPI + HTMX review dashboard.

Server-rendered HTML with a sprinkling of HTMX (vendored, no CDN, no build step). Approving or
rejecting only changes a row in the SQLite queue and appends to the audit log -- the dashboard has
no Salesforce credentials and cannot write to Salesforce. That only happens via ``sfdq apply``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sfdq.models import Confidence, FindingKind, QueuedProposal, Status
from sfdq.queue import InvalidTransition, NotFoundError, QueueStore
from sfdq.settings import Settings
from sfdq.web.views import card_view, pretty_json, short_time

HERE = Path(__file__).parent
PAGE_SIZE = 25
STATUS_TABS = [
    ("pending", "Pending"),
    ("approved", "Approved"),
    ("applied", "Applied"),
    ("rejected", "Rejected"),
    ("all", "All"),
]

# No inline scripts or styles, nothing from other origins: record data is untrusted CRM text.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def create_app(settings: Settings, store: QueueStore | None = None) -> FastAPI:
    """Build the dashboard app. ``store`` defaults to the queue DB from ``settings``."""
    store = store or QueueStore(settings.db_path)
    app = FastAPI(title="sfdq review dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["short_time"] = short_time
    templates.env.filters["pretty_json"] = pretty_json
    actor = f"web:{settings.reviewer}"

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def require_htmx(request: Request) -> None:
        """CSRF guard: browsers cannot send a custom header cross-site without CORS preflight."""
        if request.headers.get("HX-Request") != "true":
            raise HTTPException(
                status_code=403, detail="state changes must come from the dashboard"
            )

    def base_context(request: Request) -> dict[str, object]:
        return {
            "request": request,
            "mode": settings.describe(),
            "counts": store.counts(),
            "reviewer": settings.reviewer,
        }

    def render_card(
        request: Request, p: QueuedProposal, error: str | None = None, status_code: int = 200
    ) -> HTMLResponse:
        ctx = {**base_context(request), "v": card_view(p, error), "oob": True}
        card = templates.get_template("_card.html").render(ctx)
        counts = templates.get_template("_counts.html").render(ctx)
        return HTMLResponse(card + counts, status_code=status_code)

    # ---- pages -----------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def queue_page(
        request: Request,
        status: str = "pending",
        kind: str = "",
        confidence: str = "",
        object: str = "",
        page: Annotated[int, Query(ge=1)] = 1,
    ) -> HTMLResponse:
        status = status if status in dict(STATUS_TABS) else "pending"
        items = store.list_proposals(
            status=Status(status) if status in ("pending", "rejected") else None,
            kind=FindingKind(kind) if kind in {k.value for k in FindingKind} else None,
            confidence=Confidence(confidence)
            if confidence in {c.value for c in Confidence}
            else None,
            object_type=object if object in ("Account", "Contact") else None,
        )
        if status == "approved":
            items = [p for p in items if p.status is Status.APPROVED and not p.applied]
        elif status == "applied":
            items = [p for p in items if p.applied]
        total = len(items)
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(page, pages)
        window = items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        ctx = {
            **base_context(request),
            "cards": [card_view(p) for p in window],
            "tabs": STATUS_TABS,
            "filters": {"status": status, "kind": kind, "confidence": confidence, "object": object},
            "total": total,
            "page": page,
            "pages": pages,
            "kinds": [(k.value, k.value.replace("_", " ")) for k in FindingKind],
        }
        partial = request.headers.get("HX-Target") == "queue"
        return templates.TemplateResponse(request, "_queue.html" if partial else "queue.html", ctx)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, proposal: int | None = None) -> HTMLResponse:
        ctx = {
            **base_context(request),
            "entries": store.audit_entries(limit=300, proposal_id=proposal),
            "chain": store.verify_audit_chain(),
            "proposal_filter": proposal,
        }
        return templates.TemplateResponse(request, "audit.html", ctx)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "mode": settings.describe(), "queue": store.counts()})

    # ---- decisions -------------------------------------------------------------------------
    def decide(
        request: Request, proposal_id: int, new_status: Status, note: str | None
    ) -> HTMLResponse:
        try:
            p = store.decide(proposal_id, new_status, actor, note)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidTransition as exc:
            return render_card(request, store.get_proposal(proposal_id), str(exc), 409)
        return render_card(request, p)

    @app.post("/proposals/{proposal_id}/approve", dependencies=[Depends(require_htmx)])
    def approve(
        request: Request, proposal_id: int, note: Annotated[str, Form()] = ""
    ) -> HTMLResponse:
        return decide(request, proposal_id, Status.APPROVED, note)

    @app.post("/proposals/{proposal_id}/reject", dependencies=[Depends(require_htmx)])
    def reject(
        request: Request, proposal_id: int, note: Annotated[str, Form()] = ""
    ) -> HTMLResponse:
        return decide(request, proposal_id, Status.REJECTED, note)

    @app.post("/proposals/{proposal_id}/reopen", dependencies=[Depends(require_htmx)])
    def reopen(request: Request, proposal_id: int) -> HTMLResponse:
        try:
            p = store.reopen(proposal_id, actor)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidTransition as exc:
            return render_card(request, store.get_proposal(proposal_id), str(exc), 409)
        return render_card(request, p)

    return app
