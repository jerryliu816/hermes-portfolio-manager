"""The `portfolio` command.

Everything the app does is reachable without a browser. That matters for two
reasons: the extraction and reconciliation work is much easier to inspect as
text than as a screen, and a system of record you can only query through a UI
is one you cannot script, diff, or check from a cron job.

Output favours being read by a person. Money is printed with separators, and
figures that carry caveats print them.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.orm import Session

from .analytics import load_portfolio, summarize
from .analytics.policy_drift import assess
from .config import REPO_ROOT, get_settings
from .database import session_scope
from .extraction.benchmark import discover_fixtures, run_benchmark
from .extraction.contracts import ExtractionError
from .extraction.registry import get_provider
from .models.entities import PortfolioSnapshot
from .models.enums import ContextKind
from .services.advisor_context import (
    ContextError,
    add_document,
    add_note,
    add_url,
    get_context,
    list_context,
    update_context,
)
from .services.backup import BackupError, create_backup, inspect_backup, restore_backup
from .services.importer import DateOverrideError, ImportResult, import_documents
from .services.philosophy import (
    STARTER_PHILOSOPHY,
    PhilosophyError,
    active_philosophy,
    save_philosophy,
)
from .services.policy import STARTER_POLICY_YAML, PolicyError, active_policy, save_policy_yaml
from .services.recommendation import (
    DecisionType,
    RecommendationStatus,
    journal_stats,
    list_recommendations,
    record_decision,
)
from .services.snapshot_removal import (
    SnapshotRemovalError,
    find_snapshot,
    plan_removal,
    remove_snapshot,
)

app = typer.Typer(
    name="portfolio",
    help="A personal portfolio system of record.",
    no_args_is_help=True,
    add_completion=False,
)
policy_app = typer.Typer(help="Investment policy.", no_args_is_help=True)
hermes_app = typer.Typer(help="Hermes integration.", no_args_is_help=True)
journal_app = typer.Typer(help="Recommendation journal.", no_args_is_help=True)
backup_app = typer.Typer(help="Backup and restore.", no_args_is_help=True)
extraction_app = typer.Typer(help="Extraction providers.", no_args_is_help=True)
#: Bare `portfolio snapshots` still lists, so the sub-commands do not cost
#: anyone the reflex they already have.
auth_app = typer.Typer(help="Sign-in for network access.", no_args_is_help=True)
snapshots_app = typer.Typer(
    short_help="Snapshots.",
    help=(
        "Snapshots. With no sub-command, lists them newest first: as-of date, status, "
        "whether the account set was complete, total, and the start of the id. A snapshot "
        "marked superseded was replaced by a later reading of the same date and is kept so "
        "the history stays readable. The id or the date is what `remove` takes."
    ),
    invoke_without_command=True,
)
advisor_app = typer.Typer(
    help="What the advisor reads: philosophy and context.", no_args_is_help=True
)
philosophy_app = typer.Typer(help="Your investment philosophy.", no_args_is_help=True)
context_app = typer.Typer(help="Views, preferences, URLs and documents.", no_args_is_help=True)
advisor_app.add_typer(philosophy_app, name="philosophy")
advisor_app.add_typer(context_app, name="context")
app.add_typer(advisor_app, name="advisor")
app.add_typer(policy_app, name="policy")
app.add_typer(hermes_app, name="hermes")
app.add_typer(journal_app, name="journal")
app.add_typer(backup_app, name="backup")
app.add_typer(extraction_app, name="extraction")
app.add_typer(snapshots_app, name="snapshots")
app.add_typer(auth_app, name="auth")

console = Console()


def _money(value: Decimal | None) -> str:
    return "-" if value is None else f"${value:,.2f}"


def _latest_snapshot(session: Session) -> PortfolioSnapshot | None:
    """The newest snapshot that has not been superseded."""
    found: PortfolioSnapshot | None = (
        session.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.superseded_by.is_(None))
            .order_by(PortfolioSnapshot.as_of_date.desc(), PortfolioSnapshot.created_at.desc())
        )
        .scalars()
        .first()
    )
    return found


def _require_snapshot(session: Session) -> PortfolioSnapshot:
    snapshot = _latest_snapshot(session)
    if snapshot is None:
        console.print("[red]No portfolio snapshot exists yet.[/] Import a document first:")
        console.print("  portfolio import <file>")
        raise typer.Exit(1)
    return snapshot


@app.command("import")
def import_file(
    paths: Annotated[list[Path], typer.Argument(help="One or more brokerage exports: CSV or PDF")],
    provider: Annotated[
        str | None, typer.Option(help="Override EXTRACTION_PROVIDER, e.g. mock or anthropic")
    ] = None,
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help=(
                "Place these documents on YYYY-MM-DD instead of the date they state. "
                "Unnecessary when you pass both files at once: the first one filed "
                "sets the date and the rest join it."
            ),
        ),
    ] = None,
    force_as_of: Annotated[
        bool,
        typer.Option(
            "--force-as-of",
            help="Allow --as-of to move a document further than the configured limit.",
        ),
    ] = False,
) -> None:
    """Import one or more brokerage documents.

    Re-importing an identical file is a no-op: deduplication is by content
    hash, so an accidental double import cannot create a second snapshot.

    Pass both documents at once and they become one portfolio without any
    dates being typed: the first is filed, the rest join it, and the order is
    chosen for you. That order matters -- a joining document may only supply
    accounts the snapshot already has -- so the document covering the most
    accounts goes first, whatever order you list them in.

    One document at a time still works exactly as before, and `--as-of` is how
    a second one is filed onto an earlier date afterwards.
    """
    missing = [p for p in paths if not p.exists()]
    if missing:
        for path in missing:
            console.print(f"[red]No such file:[/] {path}")
        raise typer.Exit(1)

    requested_date: dt.date | None = None
    if as_of is not None:
        try:
            requested_date = dt.date.fromisoformat(as_of)
        except ValueError:
            console.print(f"[red]--as-of must be YYYY-MM-DD, not[/] {as_of!r}")
            raise typer.Exit(1) from None

    if force_as_of and requested_date is None:
        console.print("[red]--force-as-of has no meaning without --as-of.[/]")
        raise typer.Exit(1)

    settings = get_settings()
    try:
        chosen = get_provider(provider, settings)
    except ExtractionError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    with session_scope() as session:
        try:
            outcome = import_documents(
                session,
                paths,
                chosen,
                as_of_date=requested_date,
                force_as_of=force_as_of,
                settings=settings,
            )
        except DateOverrideError as exc:
            console.print(f"[red]Refused:[/] {exc}")
            console.print(
                "[yellow]Re-run with --force-as-of if you are sure the two documents "
                "describe the same portfolio.[/]"
            )
            raise typer.Exit(1) from exc
        except ExtractionError as exc:
            console.print(f"[red]Extraction failed:[/] {exc}")
            if exc.retryable:
                console.print("[yellow]This looks transient; retrying may work.[/]")
            raise typer.Exit(1) from exc

        if outcome.reordered:
            console.print(
                "[dim]filed widest first, so every account survives the join: "
                + " then ".join(outcome.order)
                + "[/]"
            )
        for result in outcome.results:
            _print_import(result, len(outcome.results) > 1)


def _print_import(result: ImportResult, several: bool) -> None:
    """Report one document's import."""
    if several:
        console.print(f"\n[bold]{result.source_file.original_filename}[/]")
    console.print(f"[bold]{result.outcome.value}[/]: {result.message}")

    snapshot = result.snapshot
    if snapshot is None:
        return

    for entry in snapshot.skipped:
        console.print(
            f"  [dim]skipped[/] {entry.display_name} "
            f"({entry.position_count} holdings, {entry.reason})"
        )
    for warning in snapshot.warnings[:10]:
        console.print(f"  [yellow]![/] {warning}")

    multi = result.multi_source
    if multi is not None:
        for decision in multi.precedence.decisions:
            console.print(f"  [dim]{decision.account_key}[/] {decision.outcome.value}")
        for name in multi.unmatched_accounts:
            console.print(
                f"  [red]not added[/] {name} - this document reports it but your "
                "portfolio does not cover it"
            )

    failures = snapshot.reconciliation.failures
    if failures:
        console.print(f"\n[red]{len(failures)} reconciliation check(s) failed:[/]")
        for check in failures[:10]:
            console.print(f"  {check.describe()}")
        console.print(
            "\n[yellow]This import needs review. The numbers above are what the "
            "document says versus what it adds up to.[/]"
        )
    if not snapshot.validation.is_complete:
        console.print(
            "\n[red]The extraction was incomplete[/] - an account identified in the "
            "document was not extracted. The remaining figures may still reconcile "
            "and be wrong."
        )


@app.command()
def summary() -> None:
    """Portfolio summary, with the caveats that qualify it.

    The total is never printed alone. It carries the date it is as of, whether
    the snapshot is complete, and a line for every caveat that qualifies it --
    a stale account, an unclassified holding, a missing cost basis. Read the
    figure and its caveats as one reading, not as a number with footnotes.

    Requires at least one imported document. Exits 1 when there is no snapshot.
    """
    with session_scope() as session:
        snapshot = _require_snapshot(session)
        view = load_portfolio(session, snapshot)
        result = summarize(view)

        console.print(
            f"\n[bold]{_money(result.total_value)}[/]  "
            f"[dim]as of {result.as_of_date} ({result.completeness})[/]"
        )
        console.print(
            f"  holdings {_money(result.holdings_value)}  cash {_money(result.cash_value)}"
        )
        console.print(
            f"  {result.account_count} accounts, {result.position_count} holdings, "
            f"{result.security_count} securities"
        )
        for caveat in result.quality.caveats():
            console.print(f"  [yellow]![/] {caveat}")
        for warning in result.warnings:
            console.print(f"  [yellow]![/] {warning}")


@app.command()
def accounts() -> None:
    """List accounts, with source and staleness.

    Source is the document that won precedence for the account, which is what
    to check when an aggregator and the custodian itself both describe it.
    Stale means the account's own source is older than the snapshot it sits in.

    Anything the documents did not say is shown as unknown rather than filled
    in with a guess.
    """
    with session_scope() as session:
        view = load_portfolio(session, _require_snapshot(session))
        table = Table(show_header=True, header_style="bold")
        for column in ("Account", "Type", "Tax", "Value", "Source", "Stale"):
            table.add_column(column, justify="right" if column == "Value" else "left")
        for row in sorted(view.accounts, key=lambda a: -a.total_value):
            table.add_row(
                row.display_name[:38],
                row.account_type,
                row.tax_treatment if row.tax_treatment != "unknown" else "[yellow]unknown[/]",
                _money(row.total_value),
                row.supplied_by or "-",
                "[yellow]yes[/]" if row.is_stale else "no",
            )
        console.print(table)


@app.command()
def holdings(
    account: Annotated[
        str | None,
        typer.Option(help="Keep only accounts whose name contains this, case-insensitively"),
    ] = None,
    limit: Annotated[int, typer.Option(help="How many rows to show")] = 25,
) -> None:
    """List holdings, largest first.

    One row per holding per account. Share counts are never summed across
    accounts, because the same symbol can be pre-split in one source and
    post-split in another; rows are ranked by market value instead.

    Closed positions are omitted. A holding with no symbol shows a dash, since
    identity here is symbol-or-name and not every security has one.
    """
    with session_scope() as session:
        view = load_portfolio(session, _require_snapshot(session))
        rows = [h for h in view.holdings if not h.is_closed]
        if account:
            needle = account.lower()
            rows = [h for h in rows if needle in h.account_name.lower()]
        rows.sort(key=lambda h: -h.value)

        table = Table(show_header=True, header_style="bold")
        for column in ("Symbol", "Name", "Account", "Value", "Class"):
            table.add_column(column, justify="right" if column == "Value" else "left")
        for row in rows[:limit]:
            table.add_row(
                row.symbol or "[dim]-[/]",
                row.security_name[:32],
                row.account_name[:26],
                _money(row.market_value),
                row.asset_class,
            )
        console.print(table)
        if len(rows) > limit:
            console.print(f"[dim]... and {len(rows) - limit} more[/]")


@policy_app.command("show")
def policy_show() -> None:
    """Print the active policy as YAML.

    The stored text is printed verbatim, comments and all, so what you read
    back is exactly what was validated. Exits 1 when no policy is set.
    """
    with session_scope() as session:
        stored = active_policy(session)
        if stored is None:
            console.print("[yellow]No policy set.[/] Start from a template:")
            console.print("  portfolio policy starter > policy.yaml")
            raise typer.Exit(1)
        console.print(stored.record.raw_yaml or "")


@policy_app.command("starter")
def policy_starter() -> None:
    """Print a commented starter policy.

    Writes nothing anywhere. Redirect it to a file, edit the targets, then
    store it with `portfolio policy set`.
    """
    print(STARTER_POLICY_YAML)


@policy_app.command("set")
def policy_set(path: Annotated[Path, typer.Argument(help="A policy YAML file")]) -> None:
    """Validate and store a policy as a new version.

    Policies are versioned, never overwritten: an earlier version stays
    readable, so an assessment made against it can still be explained later.

    A file that fails validation is rejected whole -- nothing is stored, and
    the policy that was already active stays active.
    """
    if not path.exists():
        console.print(f"[red]No such file:[/] {path}")
        raise typer.Exit(1)
    with session_scope() as session:
        try:
            stored = save_policy_yaml(session, path.read_text())
        except PolicyError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        console.print(f"[green]Saved[/] policy {stored.record.name} v{stored.record.version}")


@policy_app.command("check")
def policy_check() -> None:
    """Assess the portfolio against the policy.

    Prints drift per asset class, then any constraint breaches, then what
    qualifies the assessment.

    "provisional" is about the data, not the drift: it means a caveat makes the
    reading too uncertain to act on without checking first. A portfolio can be
    in band and still provisional, if it was assessed on stale or partly
    unclassified holdings.
    """
    with session_scope() as session:
        snapshot = _require_snapshot(session)
        stored = active_policy(session)
        if stored is None:
            console.print("[yellow]No policy set;[/] nothing to assess against.")
            raise typer.Exit(1)
        result = assess(load_portfolio(session, snapshot), stored.document)

        state = "[green]in policy[/]" if result.is_compliant else "[red]out of policy[/]"
        provisional = " [yellow](provisional)[/]" if result.is_provisional else ""
        console.print(f"\n{state}{provisional}\n")

        table = Table(show_header=True, header_style="bold")
        for column in ("Asset class", "Target", "Actual", "Drift", ""):
            table.add_column(column, justify="left" if column == "Asset class" else "right")
        for drift in result.drift:
            marker = "" if drift.is_in_band else f"[red]{drift.direction}[/]"
            table.add_row(
                drift.label,
                f"{drift.target:.1%}",
                f"{drift.actual:.1%}",
                f"{drift.drift:+.1%}",
                marker,
            )
        console.print(table)

        for breach in result.breaches:
            console.print(f"[red]breach[/] {breach.constraint.id}: {breach.detail}")
        for caveat in result.data_caveats:
            console.print(f"[yellow]![/] {caveat}")
        for note in result.policy_notes:
            console.print(f"[dim]note:[/] {note}")


@journal_app.command("list")
def journal_list(
    status: Annotated[RecommendationStatus | None, typer.Option(help="Filter by status")] = None,
    limit: Annotated[int, typer.Option(help="How many recommendations to show")] = 20,
) -> None:
    """List recommendations and their decisions.

    Each recommendation is shown with every decision recorded against it, in
    the order they were made. Ids are abbreviated to eight characters here;
    `portfolio journal decide` wants the full one.
    """
    with session_scope() as session:
        entries = list_recommendations(session, status=status, limit=limit)
        if not entries:
            console.print("[dim]No recommendations recorded.[/]")
            return
        for entry in entries:
            colour = {
                "pending": "yellow",
                "accepted": "green",
                "rejected": "red",
                "deferred": "cyan",
            }[entry.status.value]
            console.print(
                f"\n[{colour}]{entry.status.value:8}[/] [bold]{entry.recommendation.title}[/]"
            )
            console.print(
                f"  [dim]{str(entry.recommendation.id)[:8]} "
                f"from {entry.recommendation.source} "
                f"on {entry.recommendation.created_at:%Y-%m-%d}[/]"
            )
            console.print(f"  {entry.recommendation.rationale[:200]}")
            for decision in entry.decisions:
                note = f" - {decision.note}" if decision.note else ""
                console.print(f"  [dim]{decision.created_at:%Y-%m-%d} {decision.decision}{note}[/]")


@journal_app.command("decide")
def journal_decide(
    recommendation_id: Annotated[
        str, typer.Argument(help="The full recommendation id, not the abbreviated one")
    ],
    decision: Annotated[DecisionType, typer.Argument(help="accepted, rejected, or deferred")],
    note: Annotated[str | None, typer.Option(help="Why. Future-you will want this.")] = None,
) -> None:
    """Record a decision. Appends; it never replaces an earlier one.

    Changing your mind is recorded as a second decision, so the journal shows
    what was thought at the time and not only where it ended up.

    deferred is deliberately distinct from rejected: a deferred idea may
    reasonably be raised again, a rejected one may not.
    """
    with session_scope() as session:
        entry = record_decision(session, recommendation_id, decision, note=note)
        if entry is None:
            console.print(f"[red]No recommendation[/] {recommendation_id}")
            raise typer.Exit(1)
        console.print(
            f"[green]Recorded[/] {decision.value} for [bold]{entry.recommendation.title}[/]"
        )


@journal_app.command("stats")
def journal_stats_command() -> None:
    """Summarise the journal.

    Counts by status, and the acceptance rate. The rate is over decided
    recommendations only -- pending ones are not counted as rejections -- and
    is omitted entirely when nothing has been decided yet.
    """
    with session_scope() as session:
        stats = journal_stats(session)
        console.print(
            f"{stats.total} recommendation(s): "
            f"{stats.pending} pending, {stats.accepted} accepted, "
            f"{stats.rejected} rejected, {stats.deferred} deferred"
        )
        if stats.acceptance_rate is not None:
            console.print(f"acceptance rate: {stats.acceptance_rate:.0%} of those decided")


@philosophy_app.command("show")
def philosophy_show() -> None:
    """Print the active philosophy. Exits 1 when none is written."""
    with session_scope() as session:
        record = active_philosophy(session)
        if record is None:
            console.print("[yellow]No philosophy written.[/] Start from the questions:")
            console.print("  portfolio advisor philosophy starter > philosophy.md")
            raise typer.Exit(1)
        print(record.body_markdown)


@philosophy_app.command("starter")
def philosophy_starter() -> None:
    """Print the starter questions. Writes nothing."""
    print(STARTER_PHILOSOPHY)


@philosophy_app.command("set")
def philosophy_set(path: Annotated[Path, typer.Argument(help="A markdown file")]) -> None:
    """Store a philosophy as a new version; the previous one is retired."""
    if not path.exists():
        console.print(f"[red]No such file:[/] {path}")
        raise typer.Exit(1)
    with session_scope() as session:
        try:
            record = save_philosophy(session, path.read_text())
        except PhilosophyError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        console.print(f"[green]Saved[/] philosophy v{record.version}")


@context_app.command("list")
def context_list(
    archived: Annotated[bool, typer.Option(help="Include archived items")] = False,
) -> None:
    """Everything the advisor is told to take into account."""
    with session_scope() as session:
        listing = list_context(session, include_archived=archived)
        table = Table("id", "kind", "title", "text", "expires", "by")
        for label, rows in (("", listing.current), ("[red]expired[/] ", listing.expired)):
            for r in rows:
                by = r.created_by if r.confirmed else f"{r.created_by} (unconfirmed)"
                table.add_row(
                    str(r.id)[:8],
                    r.kind,
                    label + r.title,
                    r.extraction,
                    r.expires_on.isoformat() if r.expires_on else "",
                    by,
                )
        console.print(table)


@context_app.command("add")
def context_add(
    kind: Annotated[str, typer.Argument(help="view or preference")],
    title: Annotated[str, typer.Argument()],
    body: Annotated[str, typer.Argument()],
    expires: Annotated[dt.date | None, typer.Option(formats=["%Y-%m-%d"])] = None,
) -> None:
    """Record a view or a preference."""
    with session_scope() as session:
        try:
            record = add_note(
                session,
                kind=ContextKind(kind),
                title=title,
                body=body,
                expires_on=expires.date() if isinstance(expires, dt.datetime) else expires,
            )
        except (ContextError, ValueError) as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        console.print(f"[green]Added[/] {record.kind} {str(record.id)[:8]}")


@context_app.command("add-url")
def context_add_url(
    url: Annotated[str, typer.Argument()],
    note: Annotated[str, typer.Option(help="Why it matters")] = "",
) -> None:
    """Record a page for the advisor to read live."""
    with session_scope() as session:
        try:
            record = add_url(session, url=url, note=note)
        except ContextError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        console.print(f"[green]Added[/] url {str(record.id)[:8]}")


@context_app.command("add-doc")
def context_add_doc(
    path: Annotated[Path, typer.Argument(help="A PDF, .txt or .md file")],
    note: Annotated[str, typer.Option(help="Why it matters")] = "",
) -> None:
    """Store a document and report whether its text could be read."""
    if not path.exists():
        console.print(f"[red]No such file:[/] {path}")
        raise typer.Exit(1)
    with session_scope() as session:
        try:
            record = add_document(session, filename=path.name, content=path.read_bytes(), note=note)
        except ContextError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        console.print(f"[green]Added[/] document {str(record.id)[:8]} ({record.extraction})")
        if record.extraction == "no_text":
            console.print("[yellow]It has no text layer; the advisor cannot read it.[/]")


@context_app.command("archive")
def context_archive(context_id: Annotated[str, typer.Argument(help="The full id")]) -> None:
    """Stop the advisor reading an item, without deleting it."""
    with session_scope() as session:
        record = get_context(session, context_id)
        if record is None:
            console.print(f"[red]No context item {context_id}[/]")
            raise typer.Exit(1)
        update_context(session, record, is_active=False)
        console.print(f"[green]Archived[/] {record.title}")


@advisor_app.command("status")
def advisor_status() -> None:
    """Whether the advisor (Hermes's api_server) can be reached."""
    import asyncio

    from .services.hermes_client import HermesClient, HermesUnavailableError

    client = HermesClient(get_settings())
    try:
        asyncio.run(client.health())
    except HermesUnavailableError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Hermes is reachable[/] at {client.base_url}")


@hermes_app.command("install")
def hermes_install(
    api_url: Annotated[str, typer.Option(help="Where this API is reachable")] = (
        "http://localhost:8787"
    ),
    apply: Annotated[
        bool, typer.Option("--apply", help="Actually copy the skill into ~/.hermes")
    ] = False,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace a skill already installed there")
    ] = False,
) -> None:
    """Show how to connect this portfolio to Hermes.

    Prints commands by default. Modifying an agent's configuration is not
    something to do silently as a side effect of a different command.
    """
    from .hermes.install import build_plan, install_advisor_skill, install_skill

    plan = build_plan(api_url)
    print(plan.render())
    if apply:
        for install in (install_skill, install_advisor_skill):
            ok, message = install(plan, overwrite=overwrite)
            console.print(f"\n{'[green]' if ok else '[yellow]'}{message}[/]")


@snapshots_app.callback()
def snapshots(ctx: typer.Context) -> None:
    """List snapshots, newest first.

    One row per snapshot: its as-of date, status, whether the account set was
    complete, its total, and the first characters of its id. A snapshot marked
    superseded was replaced by a later reading of the same date and is kept so
    the history stays readable.

    The id or the date is what `portfolio snapshots remove`, `hide` and `show`
    take.
    """
    if ctx.invoked_subcommand is not None:
        return
    with session_scope() as session:
        records = session.execute(
            select(PortfolioSnapshot).order_by(PortfolioSnapshot.as_of_date.desc())
        ).scalars()
        table = Table(show_header=True, header_style="bold")
        for column in ("Date", "Status", "Complete", "Value", "Id"):
            table.add_column(column)
        for record in records:
            view = load_portfolio(session, record)
            table.add_row(
                str(record.as_of_date),
                record.status,
                record.completeness,
                _money(view.total_value),
                str(record.id)[:8]
                + ("  [dim](superseded)[/]" if record.superseded_by else "")
                + ("  [dim](hidden from chart)[/]" if record.hidden_from_history else ""),
            )
        console.print(table)


def _set_hidden_from_history(reference: str, hidden: bool) -> None:
    with session_scope() as session:
        try:
            snapshot = find_snapshot(session, reference)
        except SnapshotRemovalError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        snapshot.hidden_from_history = hidden
        state = "hidden from" if hidden else "shown on"
        console.print(
            f"the {snapshot.as_of_date} snapshot [dim]{str(snapshot.id)[:8]}[/] is now "
            f"{state} the value-over-time chart"
        )


@snapshots_app.command("hide")
def snapshots_hide(
    reference: Annotated[str, typer.Argument(help="A snapshot id, an id prefix, or an as-of date")],
) -> None:
    """Leave a snapshot off the value-over-time chart.

    For a snapshot whose total is only part of the portfolio -- one document
    imported on its own -- which would otherwise plot as a false dip. Nothing is
    deleted, and which snapshot is current does not change. `show` undoes it.
    """
    _set_hidden_from_history(reference, True)


@snapshots_app.command("show")
def snapshots_show(
    reference: Annotated[str, typer.Argument(help="A snapshot id, an id prefix, or an as-of date")],
) -> None:
    """Put a hidden snapshot back on the value-over-time chart."""
    _set_hidden_from_history(reference, False)


@snapshots_app.command("remove")
def snapshots_remove(
    reference: Annotated[str, typer.Argument(help="A snapshot id, an id prefix, or an as-of date")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Remove it even though something else depends on it."),
    ] = False,
) -> None:
    """Unfile a snapshot, so its documents can be imported again.

    The case this is for: two documents describing one portfolio were exported
    on different days, the second stated its own date and opened a snapshot of
    its own instead of joining the first. Imports deduplicate by content hash,
    so re-importing it with `--as-of` does nothing until the first attempt is
    unfiled.

    Accounts, securities and the preserved documents themselves are left alone.
    """
    with session_scope() as session:
        try:
            snapshot = find_snapshot(session, reference)
            plan = plan_removal(session, snapshot)
        except SnapshotRemovalError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc

        console.print(
            f"\n[bold]{snapshot.as_of_date}[/] [dim]{str(snapshot.id)[:8]}[/]  "
            f"{plan.account_count} account(s), {plan.position_count} holding(s)"
        )
        for document in plan.documents:
            console.print(f"  unfiled, and importable again: {document.original_filename}")
        for document in plan.shared_documents:
            console.print(
                f"  [dim]kept, because another snapshot uses it: {document.original_filename}[/]"
            )
        for objection in plan.objections():
            console.print(f"  [yellow]![/] {objection}")

        if plan.needs_force and not force:
            console.print("\n[red]Refusing.[/] Pass --force if that is what you want.")
            raise typer.Exit(1)
        if not yes and not typer.confirm("\nRemove it?"):
            console.print("[dim]nothing was removed[/]")
            raise typer.Exit(1)

        remove_snapshot(session, plan, force=force)
        console.print(f"[green]removed the {snapshot.as_of_date} snapshot[/]")


@backup_app.command("create")
def backup_create(
    destination: Annotated[
        Path, typer.Argument(help="A file, or a directory to write a timestamped file into")
    ] = Path("backups"),
) -> None:
    """Write a complete backup: the database and every preserved document.

    The two are one backup. The database records what each document was read to
    say; the documents are the only way to re-derive that later.
    """
    try:
        result = create_backup(destination)
    except BackupError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Wrote[/] {result.path}")
    console.print(
        f"  {result.manifest.source_file_count} document(s), "
        f"{result.size_bytes / 1_048_576:.1f} MB, database {result.manifest.database}"
    )
    console.print("  [dim]mode 0600 — it contains your original statements[/]")


@backup_app.command("inspect")
def backup_inspect(
    archive: Annotated[Path, typer.Argument(help="A backup archive written by backup create")],
) -> None:
    """Show what an archive contains, without unpacking it.

    Reads only the manifest, so it is safe to run against an archive you are
    unsure of. Use it before a restore to confirm which database and which day
    you are about to reinstate.
    """
    try:
        manifest = inspect_backup(archive)
    except BackupError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print(f"archive   {archive}")
    console.print(f"created   {manifest.created_at}")
    console.print(f"database  {manifest.database}")
    console.print(
        f"documents {manifest.source_file_count} ({manifest.source_bytes / 1_048_576:.1f} MB)"
    )
    console.print(f"format    version {manifest.version} (app {manifest.app_version})")


@backup_app.command("restore")
def backup_restore(
    archive: Annotated[Path, typer.Argument(help="A backup archive written by backup create")],
    yes: Annotated[bool, typer.Option("--yes", help="Confirm replacing current state")] = False,
) -> None:
    """Restore an archive over the current database and documents.

    Destructive and not selective: the database and every preserved document
    are replaced by the archive's, and anything imported since it was taken is
    gone. Without --yes this prints what would be replaced and changes nothing.

    Take a backup of the current state first if it holds anything the archive
    does not.
    """
    try:
        manifest = inspect_backup(archive)
    except BackupError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    settings = get_settings()
    console.print(
        f"About to replace database [bold]{settings.postgres_db}[/] and everything in "
        f"[bold]{settings.sources_dir}[/]"
    )
    console.print(
        f"  with a backup of [bold]{manifest.database}[/] taken {manifest.created_at}, "
        f"containing {manifest.source_file_count} document(s)."
    )
    if not yes:
        console.print(
            "\n[yellow]Nothing was changed.[/] This replaces state that may be newer "
            "than the backup. Re-run with --yes to proceed."
        )
        raise typer.Exit(1)

    try:
        restore_backup(archive, settings=settings, confirm=True)
    except BackupError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print("[green]Restored.[/]")


@extraction_app.command("benchmark")
def extraction_benchmark(
    provider: Annotated[
        str | None, typer.Option(help="Which provider to score, e.g. mock or anthropic")
    ] = None,
    corpus: Annotated[
        Path, typer.Option(help="Directory of documents with expected readings")
    ] = REPO_ROOT / "tests" / "fixtures" / "benchmark",
    verbose: Annotated[bool, typer.Option("--verbose", help="Show field errors")] = False,
) -> None:
    """Score a provider against the regression corpus.

    This is the objective gate for replacing one provider with another. A
    document counts as passed only if every account and position is found and
    every field matches -- "mostly right" is not a category, because the
    missing part of a portfolio is exactly the part you cannot see.
    """
    settings = get_settings()
    try:
        chosen = get_provider(provider, settings)
    except ExtractionError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    fixtures = discover_fixtures(corpus)
    if not fixtures:
        console.print(f"[yellow]No benchmark documents found in[/] {corpus}")
        console.print("  Generate expected readings with scripts/generate_expected.py")
        raise typer.Exit(1)

    capabilities = chosen.capabilities()
    if capabilities.incurs_cost:
        console.print(
            f"[yellow]{chosen.name} calls a paid API[/] — scoring {len(fixtures)} "
            "document(s) will cost money."
        )

    report = run_benchmark(fixtures, chosen, model=getattr(chosen, "model", None))

    table = Table(show_header=True, header_style="bold")
    for column in ("Document", "", "Accounts", "Positions", "Fields", "Latency"):
        table.add_column(column, justify="left" if column == "Document" else "right")
    for doc in report.documents:
        if not doc.ok:
            table.add_row(doc.name, "[red]error[/]", "—", "—", "—", f"{doc.latency_ms} ms")
            continue
        table.add_row(
            doc.name,
            "[green]pass[/]" if doc.passed else "[red]fail[/]",
            f"{doc.matched_accounts}/{doc.expected_accounts}",
            f"{doc.matched_positions}/{doc.expected_positions}",
            f"{doc.field_accuracy:.1%}",
            f"{doc.latency_ms} ms",
        )
    console.print(table)

    verdict = "[green]acceptable[/]" if report.is_acceptable else "[red]not acceptable[/]"
    console.print(
        f"\n{report.provider}"
        + (f" ({report.model})" if report.model else "")
        + f" — {verdict}: {report.passed}/{len(report.documents)} document(s) passed"
    )
    console.print(
        f"  accounts found {report.account_recall:.1%} · "
        f"positions found {report.position_recall:.1%} · "
        f"field accuracy {report.field_accuracy:.1%} on those matched"
    )
    console.print(f"  total latency {report.total_latency_ms / 1000:.1f}s")
    if report.total_cost > 0:
        console.print(
            f"  estimated cost ${report.total_cost:.4f} · "
            f"cache hit rate {report.cache_hit_rate:.1%}"
        )

    for doc in report.documents:
        if doc.error:
            console.print(f"\n[red]{doc.name}[/]: {doc.error}")
        if doc.missing_accounts:
            console.print(f"\n[red]{doc.name}[/] missed account(s): {doc.missing_accounts}")
        if doc.invented_accounts:
            console.print(f"\n[red]{doc.name}[/] invented account(s): {doc.invented_accounts}")
        if verbose and doc.field_errors:
            console.print(f"\n[yellow]{doc.name}[/] field differences:")
            for line in doc.field_errors:
                console.print(f"    {line}")

    if not report.is_acceptable:
        raise typer.Exit(1)


@app.command()
def version() -> None:
    """Print version and configuration.

    The configuration shown is what this invocation actually resolved --
    extraction provider, database and port, whether Hermes is enabled. It is
    the first thing to check when a command reads a different portfolio than
    you expected.
    """
    settings = get_settings()
    console.print("portfolio-manager 0.1.0")
    console.print(f"  extraction provider: {settings.extraction_provider}")
    console.print(f"  database: {settings.postgres_db} on port {settings.postgres_port}")
    console.print(f"  hermes enabled: {settings.hermes_enabled}")


def _set_env_values(path: Path, values: dict[str, str]) -> None:
    """Replace or append `KEY=value` lines in an env file, keeping the rest."""
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(out) + "\n")


@auth_app.command("set-password")
def auth_set_password(
    username: Annotated[str, typer.Option(prompt=True, help="The name to sign in with")],
    print_only: Annotated[
        bool, typer.Option("--print", help="Print the lines instead of writing .env")
    ] = False,
) -> None:
    """Set the sign-in username and password, writing them to .env.

    Only a scrypt hash of the password is stored. Restart the service afterwards;
    changing the password signs every existing session out.
    """
    from .auth import hash_password

    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    if len(password) < 6:
        console.print("[red]Use at least 6 characters.[/]")
        raise typer.Exit(1)
    values = {"AUTH_USERNAME": username, "AUTH_PASSWORD_HASH": hash_password(password)}
    if print_only:
        for key, value in values.items():
            typer.echo(f"{key}={value}")
        return
    env_path = REPO_ROOT / ".env"
    _set_env_values(env_path, values)
    env_path.chmod(0o600)
    console.print(f"[green]Sign-in set[/] for {username!r} in {env_path}. Restart the service.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
