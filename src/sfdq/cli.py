"""``sfdq`` command-line interface: scan -> propose -> review -> apply.

Every command prints which components are real vs mock. With no credentials configured everything
runs against the built-in fake org and canned "LLM", so the whole pipeline works out of the box.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from sfdq import __version__
from sfdq.agent import FixProposer, LLMFatalError, build_llm_client, propose_all
from sfdq.models import Finding, FindingKind, ProposalDraft
from sfdq.policy import WritePolicy
from sfdq.queue import Applier, Outcome
from sfdq.rules import RulesError, load_rules
from sfdq.runtime import Runtime, build_runtime
from sfdq.salesforce.base import SalesforceClient, SalesforceError
from sfdq.salesforce.mock import MockSalesforceClient
from sfdq.scanners import run_scan
from sfdq.settings import ConfigError, load_settings

app = typer.Typer(
    name="sfdq",
    help="Audit Salesforce data quality. AI proposes fixes; a human approves; then you apply.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()
err = Console(stderr=True)

KindOption = Annotated[
    list[FindingKind] | None,
    typer.Option("--kind", "-k", help="Limit to these finding kinds (repeatable)."),
]
RulesOption = Annotated[
    Path | None,
    typer.Option("--rules", help="Quality-rules YAML (default: bundled, or $SFDQ_RULES)."),
]


def _die(message: str, code: int = 1) -> NoReturn:
    err.print(f"[bold red]error:[/] {message}")
    raise typer.Exit(code)


def _runtime(rules_path: Path | None = None, *, banner: bool = True) -> Runtime:
    try:
        rt = build_runtime(rules_path)
    except (ConfigError, RulesError) as exc:
        _die(str(exc))
    if banner:
        console.print(f"[dim]{rt.settings.describe()}[/]")
    return rt


def _salesforce(rt: Runtime) -> SalesforceClient:
    try:
        return rt.salesforce()
    except SalesforceError as exc:
        _die(str(exc))


def _actor(rt: Runtime) -> str:
    return f"cli:{rt.settings.reviewer}"


@app.callback()
def main(
    version: Annotated[bool, typer.Option("--version", help="Show version and exit.")] = False,
) -> None:
    """sfdq: AI proposes, humans approve."""
    if version:
        console.print(f"sfdq {__version__}")
        raise typer.Exit()


# --------------------------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------------------------
@app.command()
def scan(
    kind: KindOption = None,
    rules: RulesOption = None,
    show: Annotated[int, typer.Option("--show", help="Print this many example findings.")] = 5,
) -> None:
    """Run the deterministic scanners (no LLM) and save findings to the queue database."""
    rt = _runtime(rules)
    sf = _salesforce(rt)
    report = run_scan(sf, rt.rules, as_of=date.today(), kinds=kind)
    diff = rt.store.record_scan(report.findings, _actor(rt), kinds=kind)

    table = Table(title="Findings", title_justify="left")
    table.add_column("Type")
    table.add_column("Accounts", justify="right")
    table.add_column("Contacts", justify="right")
    counts = report.counts()
    for k in kind or list(FindingKind):
        table.add_row(
            k.value.replace("_", " "),
            str(counts[("Account", k.value)]),
            str(counts[("Contact", k.value)]),
        )
    console.print(table)
    scanned = ", ".join(f"{n} {obj}s" for obj, n in report.records_scanned.items())
    console.print(
        f"Scanned {scanned}: [bold]{len(report.findings)}[/] findings "
        f"([green]{diff.new} new[/], {diff.unchanged} unchanged, {diff.reopened} reopened, "
        f"{diff.resolved} resolved since last scan)."
    )
    for f in report.findings[:show]:
        console.print(f"  [dim]-[/] {f.summary}")
    if report.findings:
        console.print("\nNext: [bold]sfdq propose[/]")


# --------------------------------------------------------------------------------------------
# propose
# --------------------------------------------------------------------------------------------
@app.command()
def propose(
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Max findings to process.")
    ] = None,
    kind: KindOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the cost confirmation.")] = False,
    rules: RulesOption = None,
) -> None:
    """Ask the agent (Claude, or the mock) to propose a fix for each new finding."""
    rt = _runtime(rules)
    todo = rt.store.findings_without_proposal(kinds=kind, limit=limit)
    if not todo:
        console.print("No findings need a proposal. Run [bold]sfdq scan[/] first, or all are done.")
        return
    llm = build_llm_client(rt.settings)
    if rt.settings.llm_mode == "real" and len(todo) > 25 and not yes:
        typer.confirm(
            f"This will send {len(todo)} findings to Claude ({llm.model}) and incur API usage. "
            "Continue? (tip: --limit N to try a few first)",
            abort=True,
        )
    proposer = FixProposer(llm, _salesforce(rt), rt.rules, WritePolicy(rt.rules))

    def show_progress(f: Finding, d: ProposalDraft) -> None:
        marker = "[yellow]fallback[/]" if d.source == "fallback" else d.action.value
        console.print(
            f"  [dim]{f.kind.value:<14}[/] {marker:<16} "
            f"[dim]{d.confidence.value:<6}[/] {f.summary}",
            no_wrap=True,
            overflow="ellipsis",
        )

    try:
        stats = propose_all(todo, proposer, rt.store, f"agent:{llm.name}", show_progress)
    except LLMFatalError as exc:
        _die(f"LLM unavailable: {exc}")
    simulated = " (simulated)" if rt.settings.llm_mode == "mock" else ""
    console.print(
        f"\nQueued [bold]{stats.proposed}[/] proposals as [yellow]pending[/] "
        f"({stats.fallbacks} used the safe fallback). "
        f"{stats.llm_calls} model calls, {stats.input_tokens:,} input / "
        f"{stats.output_tokens:,} output tokens{simulated}."
    )
    console.print("Next: [bold]sfdq review[/]  (approve or reject in the dashboard)")


# --------------------------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------------------------
@app.command()
def review(
    host: Annotated[
        str, typer.Option(help="Bind address. No auth: keep it on localhost.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
) -> None:
    """Start the review dashboard (approve / reject proposals in your browser)."""
    import uvicorn

    from sfdq.web.app import create_app

    rt = _runtime()
    if rt.settings.public_demo:
        console.print("[dim]Public demo mode: mock data only, shared by every visitor.[/]")
    elif host not in ("127.0.0.1", "localhost", "::1"):
        err.print(
            "[yellow]warning:[/] the dashboard has no authentication; "
            f"binding to {host} exposes the review queue to your network."
        )
    console.print(f"Review dashboard: [bold link]http://{host}:{port}[/]  (Ctrl+C to stop)")
    uvicorn.run(create_app(rt.settings, rt.store), host=host, port=port, log_level="warning")


# --------------------------------------------------------------------------------------------
# mcp
# --------------------------------------------------------------------------------------------
@app.command("mcp")
def mcp_server(
    transport: Annotated[
        str, typer.Option(help="'stdio' (Claude Desktop, IDEs) or 'streamable-http'.")
    ] = "stdio",
    host: Annotated[
        str, typer.Option(help="HTTP transport only. No auth: keep localhost.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP transport only.")] = 8765,
) -> None:
    """Run the MCP server so an AI client can scan, propose and review. It can never apply."""
    if transport not in ("stdio", "streamable-http"):
        _die("--transport must be 'stdio' or 'streamable-http'")
    from sfdq.mcp_server import main as run_mcp

    # stdout is the MCP protocol channel on stdio: the server logs to stderr only.
    run_mcp(transport, host, port)


# --------------------------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------------------------
@app.command()
def apply(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show exactly what WOULD change; write nothing.")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Apply at most N proposals.")
    ] = None,
    ids: Annotated[
        list[int] | None, typer.Option("--id", help="Only these proposal IDs (repeatable).")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Apply APPROVED proposals to Salesforce. Use --dry-run first. Every action is audit-logged."""
    rt = _runtime()
    ready = rt.store.approved_unapplied(ids)
    if limit is not None:
        ready = ready[:limit]
    if not ready:
        console.print("Nothing to apply: no approved proposals are waiting.")
        return
    sf = _salesforce(rt)
    if not dry_run and not yes:
        target = "LIVE Salesforce org" if rt.settings.salesforce_mode == "real" else "mock org"
        typer.confirm(f"Apply {len(ready)} approved proposal(s) to the {target}?", abort=True)

    applier = Applier(rt.store, sf, WritePolicy(rt.rules))
    results = applier.apply_approved(
        dry_run=dry_run, actor=_actor(rt), limit=limit, proposal_ids=[p.id for p in ready]
    )

    colors = {
        Outcome.APPLIED: "green",
        Outcome.DRY_RUN: "cyan",
        Outcome.CONFLICT: "yellow",
        Outcome.FAILED: "red",
    }
    table = Table(
        title="DRY RUN: nothing was written" if dry_run else "Apply results", title_justify="left"
    )
    table.add_column("#", justify="right")
    table.add_column("Outcome")
    table.add_column("Detail", overflow="fold")
    for r in results:
        detail = "\n".join(op.description for op in r.operations) or r.message
        if r.operations and r.outcome in (Outcome.CONFLICT, Outcome.FAILED):
            detail += f"\n{r.message}"
        table.add_row(str(r.proposal_id), f"[{colors[r.outcome]}]{r.outcome.value}[/]", detail)
    console.print(table)
    tally = {o: sum(r.outcome is o for r in results) for o in Outcome}
    words = {
        Outcome.DRY_RUN: "planned (dry run)",
        Outcome.APPLIED: "applied",
        Outcome.CONFLICT: "skipped (record changed)",
        Outcome.FAILED: "failed",
    }
    summary = ", ".join(f"{n} {words[o]}" for o, n in tally.items() if n)
    console.print(f"{summary}. Every action was written to the audit log (`sfdq audit`).")
    if tally[Outcome.FAILED]:
        raise typer.Exit(1)


# --------------------------------------------------------------------------------------------
# eval
# --------------------------------------------------------------------------------------------
@app.command("eval")
def eval_agent(
    output: Annotated[Path, typer.Option("--output", "-o", help="Markdown report path.")] = Path(
        "eval-report.md"
    ),
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Also write results as JSON.")
    ] = None,
    labels: Annotated[Path | None, typer.Option(help="Labeled expected-fixes JSON.")] = None,
    kind: KindOption = None,
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="Evaluate N findings.")] = None,
    mock: Annotated[
        bool, typer.Option("--mock", help="Use the mock LLM even if ANTHROPIC_API_KEY is set.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the API-cost confirmation.")
    ] = False,
    min_auto_precision: Annotated[
        float | None,
        typer.Option(help="Exit 1 if auto-fix precision is below this (0-1), e.g. as a CI gate."),
    ] = None,
    rules: RulesOption = None,
) -> None:
    """Score the agent against labeled expected fixes (precision/recall per issue type).

    Runs on a freshly generated mock org and persists nothing. Uses the mock LLM by default and
    real Claude when ANTHROPIC_API_KEY is set (pass --mock to force the mock).
    """
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

    from sfdq.agent import MockLLMClient
    from sfdq.eval import EvalError, load_labels, render_json, render_markdown, run_eval

    load_dotenv()
    try:
        settings = load_settings()
        rule_set = load_rules(rules or settings.rules_path)
        label_set = load_labels(labels)
    except (ConfigError, RulesError, OSError, ValueError) as exc:
        _die(f"cannot start the eval: {exc}")
    llm = MockLLMClient() if mock or settings.llm_mode == "mock" else build_llm_client(settings)
    console.print(f"[dim]Eval agent: {llm.name} ({llm.model}); dataset: seeded mock org[/]")
    planned = min(limit or len(label_set.fixes), len(label_set.fixes))
    if llm.name != "mock" and planned > 25 and not yes:
        typer.confirm(
            f"This evaluates up to {planned} findings with Claude ({llm.model}) and incurs API "
            "usage. Continue? (tip: --limit N to try a few first)",
            abort=True,
        )
    try:
        with Progress(
            TextColumn("Proposing fixes"), BarColumn(), MofNCompleteColumn(), console=console
        ) as bar:
            task = bar.add_task("", total=planned)
            result = run_eval(
                rules=rule_set,
                llm=llm,
                labels=label_set,
                kinds=kind,
                limit=limit,
                on_progress=lambda i, n, f: bar.update(task, total=n, completed=i),
            )
    except LLMFatalError as exc:
        _die(f"LLM unavailable: {exc}")
    except EvalError as exc:
        _die(str(exc))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(result), "utf-8")
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(render_json(result), "utf-8")

    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.0%}"

    table = Table(title="Eval results", title_justify="left")
    for col in ("Issue type", "Seeded", "Detect P/R", "Fix P", "Fix R", "Flag", "Unsafe"):
        table.add_column(col, justify="left" if col == "Issue type" else "right", no_wrap=True)
    groups = {**result.metrics.by_issue_type, "all": result.metrics.overall}
    for name, g in groups.items():
        r = g.rates
        table.add_row(
            name.replace("_", " "),
            str(g.labeled),
            f"{pct(r['detection_precision'])}/{pct(r['detection_recall'])}",
            pct(r["auto_precision"]),
            pct(r["auto_recall"]),
            pct(r["flag_accuracy"]),
            str(g.unsafe_auto),
        )
    console.print(table)
    console.print(f"Report written to [bold]{output}[/]" + (f" and {json_out}" if json_out else ""))
    console.print(
        "[dim]Fix P/R = precision/recall of automated fixes (merge, update). Flag = asked the "
        "owner when it should. Unsafe = auto-fixed what needed a human.[/]"
    )
    precision = result.metrics.overall.rates["auto_precision"]
    if min_auto_precision is not None:
        if precision is None:
            console.print(
                "[yellow]No automated proposals in this run: precision gate not applicable.[/]"
            )
        elif precision < min_auto_precision:
            err.print(
                f"[bold red]auto-fix precision {pct(precision)} is below the required "
                f"{min_auto_precision:.0%}[/]"
            )
            raise typer.Exit(1)


# --------------------------------------------------------------------------------------------
# status / audit / reset
# --------------------------------------------------------------------------------------------
@app.command()
def status() -> None:
    """Show queue counters and audit-log integrity."""
    rt = _runtime()
    counts = rt.store.counts()
    findings = rt.store.count_findings()
    table = Table(title="Queue", title_justify="left")
    table.add_column("State")
    table.add_column("Proposals", justify="right")
    for key in ("pending", "approved", "rejected", "applied", "awaiting_apply"):
        table.add_row(key.replace("_", " "), str(counts[key]))
    console.print(table)
    breakdown = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(findings.items()))
    console.print(
        f"Open findings: {sum(findings.values())} ({breakdown or 'none'}); "
        f"without a proposal yet: {len(rt.store.findings_without_proposal())}."
    )
    chain = rt.store.verify_audit_chain()
    verdict = "[green]OK[/]" if chain.ok else "[red]BROKEN[/]"
    console.print(f"Audit log: {chain.entries} entries, hash chain {verdict}.")


@app.command()
def audit(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Number of entries.")] = 20,
    proposal: Annotated[int | None, typer.Option("--proposal", help="Only this proposal.")] = None,
) -> None:
    """Show the audit log (newest first) and verify its hash chain."""
    rt = _runtime()
    table = Table(title="Audit log", title_justify="left")
    for col in ("#", "When (UTC)", "Who", "Event", "Proposal", "Details"):
        table.add_column(col, overflow="fold", no_wrap=col in ("#", "Event", "Proposal"))
    for e in rt.store.audit_entries(limit, proposal):
        shown = {k: v for k, v in e.details.items() if k != "operations" and v is not None}
        short = ", ".join(f"{k}={v}" for k, v in shown.items())[:90]
        table.add_row(
            str(e.id),
            e.ts[:19].replace("T", " "),
            e.actor,
            e.event,
            str(e.proposal_id or ""),
            short,
        )
    console.print(table)
    chain = rt.store.verify_audit_chain()
    if chain.ok:
        console.print(f"[green]Hash chain verified[/] ({chain.entries} entries).")
    else:
        err.print(
            f"[bold red]Hash chain BROKEN at entry #{chain.first_bad_id}: the log was altered.[/]"
        )
        raise typer.Exit(1)


@app.command("seed-demo")
def seed_demo_cmd(
    force: Annotated[
        bool, typer.Option("--force", help="Wipe existing demo state and start over.")
    ] = False,
) -> None:
    """Prepare sample data for the dashboard: scan the mock org and queue proposals (idempotent)."""
    from sfdq.demo import seed_demo

    rt = _runtime()
    try:
        summary = seed_demo(rt.settings, rt.rules, force=force)
    except ConfigError as exc:
        _die(str(exc))
    if summary.seeded:
        console.print(
            f"Demo seeded: {summary.findings} findings, {summary.pending} pending proposals."
        )
    else:
        console.print(
            f"Demo data already present ({summary.findings} findings, {summary.pending} pending). "
            "Use --force to start over."
        )


@app.command("reset-demo")
def reset_demo(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Regenerate the mock org and wipe the queue (mock mode only). Handy for demos."""
    rt = _runtime()
    if rt.settings.salesforce_mode != "mock":
        _die("reset-demo only works against the mock org; refusing to touch a real Salesforce.")
    if not yes:
        typer.confirm(
            f"Delete {rt.settings.home}/ (mock org + review queue + audit log)?", abort=True
        )
    for path in (rt.settings.db_path, rt.settings.mock_org_path):
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
    MockSalesforceClient.load_or_create(rt.settings.mock_org_path)
    console.print("Demo state reset. Start again with [bold]sfdq scan[/].")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
