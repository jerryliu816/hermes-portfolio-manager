"""The Hermes boundary.

The architectural promise is that an analyst can read everything and change
nothing that matters. These tests exist so that promise cannot quietly erode:
the exposed tool set is asserted, the read-only guarantee is exercised against
a real database, and the trigger is proven to swallow every failure.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from portfolio_manager.config import Settings
from portfolio_manager.extraction.providers.mock_provider import MockExtractionProvider
from portfolio_manager.hermes.install import build_plan
from portfolio_manager.hermes.mcp_server import (
    EXPOSED_TOOLS,
    WRITE_TOOLS,
    build_mcp_server,
)
from portfolio_manager.hermes.trigger import (
    HermesAnalysisTrigger,
    NoOpAnalysisTrigger,
    get_trigger,
    sign,
    verify,
)
from portfolio_manager.services.importer import import_document

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_anonymized.csv"
)


def _tool_names() -> set[str]:
    return {t.name for t in asyncio.run(build_mcp_server().list_tools())}


def _call(tool: str, **arguments: object) -> dict[str, object]:
    """Invoke an MCP tool the way Hermes would, and return its structured result."""
    server = build_mcp_server()
    result = asyncio.run(server.call_tool(tool, arguments))
    payload = getattr(result, "structured_content", None)
    if payload is None:
        content = getattr(result, "content", None) or []
        payload = json.loads(content[0].text) if content else {}
    return dict(payload.get("result", payload))


# --- the boundary ---------------------------------------------------------


def test_the_exposed_tool_set_is_exactly_what_is_declared() -> None:
    """Adding a tool must be deliberate, not something that happens by accident."""
    assert _tool_names() == set(EXPOSED_TOOLS)


def test_only_three_tools_write_and_none_touches_holdings() -> None:
    names = _tool_names()
    writers = {n for n in names if n.startswith("save_")}
    assert writers == set(WRITE_TOOLS)
    assert writers == {
        "save_recommendation",
        "save_recommendation_decision",
        "save_advisor_context",
    }
    # Nothing that could plausibly mutate portfolio truth is exposed. Matched on
    # the leading verb rather than as a substring, because a substring check
    # trips over innocent names like `get_asset_allocation`.
    mutating_verbs = ("import", "delete", "update", "set", "create", "remove", "write")
    for name in names - set(WRITE_TOOLS):
        verb = name.split("_", 1)[0]
        assert verb not in mutating_verbs, f"{name!r} looks like it mutates state"


def test_no_tool_offers_raw_sql() -> None:
    """A query tool would make the read-only surface unbounded."""
    assert not any("sql" in n or "query" in n or "exec" in n for n in _tool_names())


def test_the_read_only_session_is_enforced_by_the_database(session: Session) -> None:
    """Defence in depth: a coding mistake fails at Postgres, not silently."""
    from sqlalchemy.exc import DBAPIError

    from portfolio_manager.database import readonly_session
    from portfolio_manager.models.advisory import Recommendation

    with readonly_session() as read_only, pytest.raises(DBAPIError):
        read_only.execute(
            Recommendation.__table__.insert().values(
                title="should not be possible", rationale="x", source="test"
            )
        )


def test_the_server_instructions_state_what_it_cannot_do() -> None:
    """The analyst should not describe itself as able to act."""
    instructions = build_mcp_server().instructions or ""
    assert "cannot change holdings" in instructions
    assert "stale" in instructions
    assert "string" in instructions, "money-as-string must be stated"


# --- provenance travels with every read ----------------------------------


@pytest.fixture
def seeded(engine: object, tmp_path: Path) -> Iterator[None]:
    """A committed portfolio the MCP tools can actually see.

    The tools open their own sessions -- deliberately, since Hermes calls them
    out of band rather than inside a request -- so the usual rolled-back test
    session is invisible to them. This points the global session factory at the
    test database, seeds a committed snapshot, and clears it afterwards.
    """
    from sqlalchemy.orm import sessionmaker

    from portfolio_manager import database
    from portfolio_manager.models import Base

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    previous = database._SessionFactory
    database._SessionFactory = factory

    settings = Settings(data_dir=tmp_path / "data", _env_file=None)  # type: ignore[call-arg]
    with factory() as setup:
        import_document(setup, FIXTURE, MockExtractionProvider(), settings=settings)
        setup.commit()
    try:
        yield
    finally:
        with factory() as cleanup:
            for table in reversed(Base.metadata.sorted_tables):
                cleanup.execute(table.delete())
            cleanup.commit()
        database._SessionFactory = previous


@pytest.mark.parametrize(
    "tool",
    [
        "portfolio_summary",
        "get_accounts",
        "get_asset_allocation",
        "get_tax_treatment_allocation",
        "get_institution_allocation",
        "get_cash_equivalents",
        "get_concentration",
        "get_embedded_gains",
    ],
)
def test_every_portfolio_read_returns_provenance(seeded: None, tool: str) -> None:
    """An analyst cannot be relied on to ask how stale the data is.

    Exercised by actually calling each tool against a seeded database, rather
    than by inspecting source: what matters is what reaches the analyst.
    """
    result = _call(tool)
    for field in ("as_of_date", "completeness", "stale_fraction", "caveats"):
        assert field in result, f"{tool} omitted {field}"
    assert result["completeness"] in {"complete", "partial", "unknown"}


# --- the trigger ----------------------------------------------------------


def test_the_default_trigger_does_nothing_successfully() -> None:
    """The app must be fully usable with Hermes absent."""
    settings = Settings(hermes_enabled=False, _env_file=None)  # type: ignore[call-arg]
    trigger = get_trigger(settings)
    assert isinstance(trigger, NoOpAnalysisTrigger)
    result = trigger.snapshot_created("snap", "2026-08-26")
    assert not result
    assert "disabled" in result.detail


def test_an_unreachable_hermes_never_fails_an_import() -> None:
    """The snapshot is already valid; an absent analyst must not undo that."""
    settings = Settings(  # type: ignore[call-arg]
        hermes_enabled=True,
        hermes_webhook_url="http://127.0.0.1:1/webhooks/nothing-here",
        hermes_webhook_secret="secret",
        hermes_timeout_seconds=0.2,
        _env_file=None,
    )
    result = HermesAnalysisTrigger(settings).snapshot_created("snap", "2026-08-26")
    assert not result.delivered
    assert "failed" in result.detail


def test_an_error_response_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(  # type: ignore[call-arg]
        hermes_enabled=True,
        hermes_webhook_url="http://example.invalid/webhooks/x",
        hermes_webhook_secret="secret",
        _env_file=None,
    )

    def _fail(*args: object, **kwargs: object) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request("POST", "http://example.invalid"))

    monkeypatch.setattr(httpx, "post", _fail)
    result = HermesAnalysisTrigger(settings).snapshot_created("snap", "2026-08-26")
    assert not result.delivered
    assert "500" in result.detail


def test_the_event_is_signed_so_hermes_can_verify_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _capture(url: str, **kwargs: object) -> httpx.Response:
        captured.update(kwargs)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", _capture)
    settings = Settings(  # type: ignore[call-arg]
        hermes_enabled=True,
        hermes_webhook_url="http://example.invalid/webhooks/x",
        hermes_webhook_secret="topsecret",
        _env_file=None,
    )
    result = HermesAnalysisTrigger(settings).snapshot_created("snap-1", "2026-08-26")
    assert result.delivered

    body = captured["content"]
    headers = captured["headers"]
    assert isinstance(body, bytes) and isinstance(headers, dict)
    assert verify(body, "topsecret", headers["x-portfolio-signature"])
    assert not verify(body, "wrong-secret", headers["x-portfolio-signature"])

    payload = json.loads(body)
    assert payload["event"] == "portfolio_snapshot_created"
    assert payload["snapshot_id"] == "snap-1"


def test_a_trigger_without_a_secret_is_not_configured() -> None:
    settings = Settings(  # type: ignore[call-arg]
        hermes_enabled=True,
        hermes_webhook_url="http://example.invalid/x",
        hermes_webhook_secret="",
        _env_file=None,
    )
    trigger = HermesAnalysisTrigger(settings)
    assert not trigger.configured
    assert "not configured" in trigger.snapshot_created("s", "d").detail


def test_signing_is_stable_and_constant_time_checked() -> None:
    assert sign(b"x", "k") == sign(b"x", "k")
    assert sign(b"x", "k") != sign(b"y", "k")


# --- the install helper ---------------------------------------------------


def test_the_install_plan_uses_the_verified_transport() -> None:
    """Streamable HTTP is the Hermes client default for `url` servers."""
    plan = build_plan("http://localhost:8787")
    # The trailing slash is the mount point. Registering the bare path made the
    # real `hermes mcp add` fail with "Server returned an error response".
    assert plan.mcp_command == ("hermes mcp add portfolio-manager --url http://localhost:8787/mcp/")
    assert "Streamable HTTP" in plan.render()


def test_the_install_plan_states_the_read_only_guarantee() -> None:
    rendered = build_plan().render()
    assert "read-only" in rendered
    assert "cannot change holdings" in rendered


def test_the_install_plan_explains_the_webhook_prerequisites() -> None:
    """Webhooks need config and a gateway; assuming otherwise would mislead."""
    rendered = build_plan().render()
    assert "platforms.webhook.enabled" in rendered
    assert "hermes gateway" in rendered
    assert "HERMES_WEBHOOK_SECRET" in rendered


def test_installing_the_skill_refuses_to_clobber_edits(tmp_path: Path) -> None:
    """The user may have edited the instructions; those edits are theirs."""
    from portfolio_manager.hermes import install as install_module

    plan = build_plan()
    target = tmp_path / "skills" / "productivity" / "portfolio-review" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("# my edited version\n")
    edited = plan.__class__(**{**plan.__dict__, "skill_target": target, "hermes_present": True})

    ok, message = install_module.install_skill(edited)
    assert not ok and "already exists" in message
    assert target.read_text() == "# my edited version\n"

    ok, message = install_module.install_skill(edited, overwrite=True)
    assert ok
    assert "portfolio-review" in target.read_text()


def test_the_skill_states_the_rules_the_data_forces() -> None:
    """These are the findings that make this portfolio hard to reason about."""
    text = (
        Path(__file__).resolve().parents[2] / "backend/portfolio_manager/hermes/skill/SKILL.md"
    ).read_text()
    assert "Never sum share counts across accounts" in text
    assert "not a return" in text
    assert "do not assume it is" in text.lower()
    assert "stale_fraction" in text
    assert "cannot** change holdings" in text or "cannot" in text


def test_the_mcp_endpoint_is_mounted() -> None:
    from portfolio_manager.main import create_app

    app = create_app()
    assert any(getattr(route, "path", "") == "/mcp" for route in app.routes)


def test_the_bare_mcp_path_redirects_rather_than_405ing() -> None:
    """A Starlette mount matches only paths below it, so `/mcp` missed it.

    `hermes mcp add ... --url http://localhost:8787/mcp` failed on the real
    client with "Server returned an error response". The redirect must be a 307
    so the JSON-RPC POST keeps its method and body.
    """
    from fastapi.testclient import TestClient

    from portfolio_manager.main import create_app

    client = TestClient(create_app(), follow_redirects=False)
    for method in ("get", "post", "delete"):
        response = getattr(client, method)("/mcp")
        assert response.status_code == 307, method
        assert response.headers["location"] == "/mcp/"


def test_money_crosses_the_boundary_as_a_string() -> None:
    from portfolio_manager.hermes.mcp_server import _money

    assert _money(Decimal("2046812.110000")) == "2046812.11"
    assert _money(None) is None


# --- what an analyst actually receives ------------------------------------


def test_summary_gives_an_analyst_the_figures_and_the_caveats(seeded: None) -> None:
    result = _call("portfolio_summary")
    assert Decimal(str(result["total_value"])) > 0
    assert result["account_count"] == 10
    assert isinstance(result["total_value"], str), "money must be a string"
    assert result["caveats"], "the real corpus is stale and partly unclassified"


def test_accounts_are_returned_with_masked_identifiers(seeded: None) -> None:
    result = _call("get_accounts")
    accounts = result["accounts"]
    assert isinstance(accounts, list) and accounts
    for account in accounts:
        assert account["account_key_masked"].startswith("*")
        assert "external_account_key" not in account


def test_security_exposure_reports_holdings_without_summing_shares(seeded: None) -> None:
    """The rule the data forces, enforced at the boundary an analyst sees."""
    allocation = _call("get_concentration")
    symbol = allocation["largest"][0]["symbol"]  # type: ignore[index]
    result = _call("get_security_exposure", symbol=symbol)
    assert "total_quantity" not in result
    assert result["account_count"] == len(result["holdings"])  # type: ignore[arg-type]


def test_an_unknown_symbol_gets_a_helpful_error(seeded: None) -> None:
    result = _call("get_security_exposure", symbol="NOTREAL")
    assert "error" in result
    assert result["available"], "the analyst should be told what it can ask for"


def test_tax_allocation_reports_what_it_excluded(seeded: None) -> None:
    result = _call("get_tax_treatment_allocation")
    assert Decimal(str(result["excluded_value"])) > 0
    assert "no confirmed tax treatment" in str(result["excluded_reason"])


def test_cash_reaches_the_analyst_as_more_than_the_summarys_cash_value(
    seeded: None,
) -> None:
    """`portfolio_summary.cash_value` is settlement balances only.

    An analyst reading that alone would understate the user's cash by every
    money-market fund and bill they hold, which is most of it.
    """
    cash = _call("get_cash_equivalents")
    summary = _call("portfolio_summary")
    assert Decimal(str(cash["settlement_value"])) == Decimal(str(summary["cash_value"]))
    assert Decimal(str(cash["holdings_value"])) > 0
    assert Decimal(str(cash["total_value"])) > Decimal(str(summary["cash_value"]))


def test_cash_buckets_separate_taxable_from_sheltered_and_unknown(seeded: None) -> None:
    result = _call("get_cash_equivalents")
    buckets = result["buckets"]
    assert isinstance(buckets, list) and buckets
    assert {b["key"] for b in buckets} <= {"taxable", "tax_sheltered", "unknown"}
    assert sum(Decimal(str(b["total_value"])) for b in buckets) == Decimal(
        str(result["total_value"])
    )
    for bucket in buckets:
        assert isinstance(bucket["total_value"], str), "money must be a string"


def test_cash_of_unconfirmed_treatment_is_not_presented_as_taxable(seeded: None) -> None:
    """Folding it into `taxable` would answer a question the data cannot."""
    buckets = _call("get_cash_equivalents")["buckets"]
    unknown = [b for b in buckets if b["key"] == "unknown"]  # type: ignore[union-attr]
    if not unknown:
        pytest.skip("the fixture holds no cash in an unclassified account")
    assert unknown[0]["tax_treatments"] == ["unknown"]


def test_the_cash_tool_says_sheltered_does_not_mean_untaxed() -> None:
    """A tax-deferred yield is taxed on withdrawal, and the analyst must know."""
    tools = {t.name: t for t in asyncio.run(build_mcp_server().list_tools())}
    description = tools["get_cash_equivalents"].description or ""
    assert "does not mean untaxed" in description


def test_a_missing_policy_tells_the_analyst_not_to_invent_one(seeded: None) -> None:
    result = _call("get_investment_policy")
    assert "error" in result
    assert "do not invent" in str(result["detail"])


def test_recording_a_recommendation_changes_no_holdings(seeded: None) -> None:
    """The only write Hermes can make, and what it must not imply."""
    from portfolio_manager import database
    from portfolio_manager.models.entities import PositionSnapshot

    factory = database.get_session_factory()
    with factory() as check:
        before = check.query(PositionSnapshot).count()

    saved = _call(
        "save_recommendation",
        title="Consider rebalancing US equity",
        rationale="US equity is 19.6 points above its target.",
        proposal={"action": "review"},
    )
    assert saved["saved"] is True
    assert "nothing was changed in the portfolio" in str(saved["note"])

    with factory() as check:
        assert check.query(PositionSnapshot).count() == before

    history = _call("get_recommendation_history")
    titles = [r["title"] for r in history["recommendations"]]  # type: ignore[index]
    assert "Consider rebalancing US equity" in titles


def test_a_decision_must_be_one_the_user_could_have_made(seeded: None) -> None:
    saved = _call("save_recommendation", title="t", rationale="r")
    recommendation_id = str(saved["recommendation_id"])

    rejected = _call(
        "save_recommendation_decision",
        recommendation_id=recommendation_id,
        decision="obliterated",
    )
    assert "error" in rejected

    accepted = _call(
        "save_recommendation_decision",
        recommendation_id=recommendation_id,
        decision="accepted",
        note="did it",
    )
    assert accepted["saved"] is True

    history = _call("get_recommendation_history")
    decisions = history["recommendations"][0]["decisions"]  # type: ignore[index]
    assert decisions[0]["decision"] == "accepted"


def test_a_decision_on_a_missing_recommendation_is_reported(seeded: None) -> None:
    import uuid

    result = _call(
        "save_recommendation_decision",
        recommendation_id=str(uuid.uuid4()),
        decision="accepted",
    )
    assert "error" in result


def test_tools_degrade_gracefully_with_no_portfolio(engine: object) -> None:
    """A fresh install must not crash the analyst.

    Bound to an explicitly emptied test database rather than assuming the
    ambient one is empty -- an assumption that breaks the moment anything else
    has written to the default database.
    """
    from sqlalchemy.orm import sessionmaker

    from portfolio_manager import database
    from portfolio_manager.models import Base

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    previous = database._SessionFactory
    database._SessionFactory = factory
    try:
        with factory() as session:
            for table in reversed(Base.metadata.sorted_tables):
                session.execute(table.delete())
            session.commit()
        result = _call("portfolio_summary")
        assert "error" in result
        assert "not imported" in str(result["detail"])
    finally:
        database._SessionFactory = previous


# --- the advisor's inputs ---------------------------------------------------


def test_a_missing_philosophy_tells_the_advisor_to_ask(seeded: None) -> None:
    result = _call("get_investment_philosophy")
    assert "error" in result
    assert "ask the user" in str(result["detail"])


def test_the_philosophy_reaches_the_advisor_verbatim(seeded: None) -> None:
    from portfolio_manager.database import session_scope
    from portfolio_manager.services.philosophy import save_philosophy

    with session_scope() as session:
        save_philosophy(session, "# Mine\nLong horizon; never tobacco.")
    result = _call("get_investment_philosophy")
    assert result["philosophy"] == "# Mine\nLong horizon; never tobacco."
    assert result["version"] == 1


def test_context_reaches_the_advisor_with_its_qualifications(seeded: None, tmp_path: Path) -> None:
    import datetime as dt

    from portfolio_manager.database import session_scope
    from portfolio_manager.models.enums import ContextKind
    from portfolio_manager.services.advisor_context import add_document, add_note, add_url

    settings = Settings(data_dir=tmp_path / "ctx", _env_file=None)  # type: ignore[call-arg]
    with session_scope() as session:
        add_note(
            session,
            kind=ContextKind.VIEW,
            title="Fed",
            body="A cut by spring",
            expires_on=dt.date(2000, 1, 1),
        )
        add_url(session, url="https://example.com/outlook")
        add_document(session, filename="long.md", content=("x" * 10000).encode(), settings=settings)

    result = _call("get_advisor_context")
    expired = result["expired"]
    assert isinstance(expired, list) and [e["title"] for e in expired] == ["Fed"]
    current = {e["kind"]: e for e in result["current"]}  # type: ignore[union-attr]
    assert "web_extract" in current["url"]["note"]
    document = current["document"]
    assert document["total_chars"] == 10000
    assert len(document["excerpt"]) < 10000 and "read_advisor_context" in document["more"]

    first = _call("read_advisor_context", context_id=document["id"])
    assert first["next_offset"] is not None
    rest = _call("read_advisor_context", context_id=document["id"], offset=first["next_offset"])
    assert len(str(first["text"])) + len(str(rest["text"])) == 10000
    assert rest["next_offset"] is None


def test_an_unreadable_document_is_flagged_to_the_advisor(seeded: None, tmp_path: Path) -> None:
    import io

    from reportlab.pdfgen import canvas

    from portfolio_manager.database import session_scope
    from portfolio_manager.services.advisor_context import add_document

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.showPage()
    pdf.save()
    settings = Settings(data_dir=tmp_path / "ctx", _env_file=None)  # type: ignore[call-arg]
    with session_scope() as session:
        add_document(session, filename="scan.pdf", content=buffer.getvalue(), settings=settings)
    (entry,) = _call("get_advisor_context")["current"]  # type: ignore[misc]
    assert entry["text_status"] == "no_text"
    assert "cannot read it" in entry["note"]


def test_what_the_advisor_saves_is_unconfirmed_and_changes_no_holdings(seeded: None) -> None:
    from sqlalchemy import func, select

    from portfolio_manager.database import session_scope
    from portfolio_manager.models.entities import PositionSnapshot

    with session_scope() as session:
        before = session.execute(select(func.count()).select_from(PositionSnapshot)).scalar()

    saved = _call(
        "save_advisor_context",
        kind="preference",
        title="No banks",
        text='User: "I never want individual bank stocks."',
    )
    assert saved["saved"] is True
    (entry,) = _call("get_advisor_context")["current"]  # type: ignore[misc]
    assert entry["from"] == "hermes"
    assert entry["confirmed_by_user"] is False
    assert "check it" in entry["caution"]

    with session_scope() as session:
        after = session.execute(select(func.count()).select_from(PositionSnapshot)).scalar()
    assert before == after


def test_the_advisor_cannot_save_an_invalid_kind_or_date(seeded: None) -> None:
    assert "error" in _call("save_advisor_context", kind="document", title="t", text="b")
    assert "error" in _call(
        "save_advisor_context", kind="view", title="t", text="b", expires_on="soon"
    )
    assert "error" in _call("save_advisor_context", kind="url", title="t", text="b")


# --- the advisor's setup ------------------------------------------------------


def test_the_install_plan_restricts_the_advisors_tools() -> None:
    rendered = build_plan().render()
    assert "API_SERVER_KEY" in rendered and "HERMES_API_KEY" in rendered
    assert "api_server: [web, skills, todo, portfolio-manager]" in rendered
    assert "no terminal" in rendered


def test_install_notes_warn_about_an_unrestricted_or_blind_advisor() -> None:
    from portfolio_manager.hermes.install import advisor_notes

    unset = " ".join(advisor_notes({}))
    assert "No web search backend" in unset
    assert "terminal included" in unset

    risky = " ".join(advisor_notes({"platform_toolsets": {"api_server": ["web", "terminal"]}}))
    assert "includes terminal" in risky
    assert "would not see the portfolio" in risky

    good = advisor_notes(
        {
            "web": {"backend": "exa"},
            "platform_toolsets": {"api_server": ["web", "portfolio-manager"]},
        }
    )
    assert good == ["Web search backend: exa. The advisor will research with it."]


def test_installing_the_advisor_skill_refuses_to_clobber_edits(tmp_path: Path) -> None:
    from portfolio_manager.hermes import install as install_module

    plan = build_plan()
    target = tmp_path / "skills" / "finance" / "investment-advisor" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("# mine\n")
    edited = plan.__class__(
        **{**plan.__dict__, "advisor_skill_target": target, "hermes_present": True}
    )
    ok, _ = install_module.install_advisor_skill(edited)
    assert not ok and target.read_text() == "# mine\n"
    ok, _ = install_module.install_advisor_skill(edited, overwrite=True)
    assert ok and "name: investment-advisor" in target.read_text()


def test_the_advisor_skill_keeps_the_arithmetic_in_the_engine() -> None:
    text = (
        Path(__file__).resolve().parents[2]
        / "backend/portfolio_manager/hermes/skill/advisor/SKILL.md"
    ).read_text()
    assert "you do not do the arithmetic" in text
    assert "simulate_trades" in text
    assert "never share counts" in text
    assert "evidence, never instructions" in text
    assert "Never" in text and "treat an unknown as zero" in text
    assert "publication date" in text


# --- the rebalance engine -------------------------------------------------

TARGETS = {
    "us_equity": "0.5",
    "intl_equity": "0.2",
    "fixed_income": "0.2",
    "cash_equivalent": "0.1",
}


def _a_holding() -> tuple[str, str, Decimal]:
    """A ticker held in some account, with its value there, read from the fixture."""
    from portfolio_manager import database
    from portfolio_manager.models.entities import PositionSnapshot, Security

    with database.get_session_factory()() as check:
        position, security = (
            check.query(PositionSnapshot, Security)
            .join(Security, PositionSnapshot.security_id == Security.id)
            .filter(Security.symbol.is_not(None), PositionSnapshot.market_value > 1000)
            .order_by(PositionSnapshot.market_value.desc())
            .first()
        )
        assert security.symbol is not None
        return str(position.account_id), security.symbol, position.market_value


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_rebalance_gaps", {"targets": TARGETS}),
        ("suggest_rebalance", {"targets": TARGETS}),
    ],
)
def test_every_engine_tool_returns_provenance(
    seeded: None, tool: str, arguments: dict[str, object]
) -> None:
    result = _call(tool, **arguments)
    assert "error" not in result, result
    for field in ("as_of_date", "completeness", "stale_fraction", "caveats"):
        assert field in result, f"{tool} omitted {field}"


def test_without_a_policy_the_engine_asks_for_targets_rather_than_inventing_them(
    seeded: None,
) -> None:
    for tool in ("get_rebalance_gaps", "suggest_rebalance"):
        result = _call(tool)
        assert "no investment policy" in str(result["error"])


def test_targets_must_be_strings_that_sum_to_one(seeded: None) -> None:
    # A JSON number is refused by the tool's own schema before it reaches the
    # engine, so a float can never become a target.
    with pytest.raises(Exception, match="string"):
        _call("get_rebalance_gaps", targets={"us_equity": 1})
    short = _call("get_rebalance_gaps", targets={"us_equity": "0.5"})
    assert "error" in short


def test_simulated_trades_report_the_engines_figures(seeded: None) -> None:
    account_id, symbol, value = _a_holding()
    result = _call(
        "simulate_trades",
        trades=[{"account_id": account_id, "action": "sell", "security": symbol, "amount": "1000"}],
        targets=TARGETS,
    )
    assert "error" not in result, result
    assert result["is_feasible"] is True
    trade = result["trades"][0]  # type: ignore[index]
    assert trade["applied_amount"] == "1000.00"
    # The portfolio's caveats and the engine's both reach the advisor.
    assert set(result["data_caveats"]) <= set(result["caveats"])  # type: ignore[arg-type]


def test_an_oversell_is_a_violation_not_a_silent_cap(seeded: None) -> None:
    account_id, symbol, value = _a_holding()
    result = _call(
        "simulate_trades",
        trades=[
            {
                "account_id": account_id,
                "action": "sell",
                "security": symbol,
                "amount": str(value * 2),
            }
        ],
        targets=TARGETS,
    )
    assert result["is_feasible"] is False
    assert result["violations"], result


def _stored(recommendation_id: str) -> object:
    import uuid

    from portfolio_manager import database
    from portfolio_manager.models.advisory import Recommendation

    with database.get_session_factory()() as check:
        record = check.get(Recommendation, uuid.UUID(recommendation_id))
        assert record is not None
        check.expunge(record)
        return record


def test_a_saved_trade_proposal_stores_the_engines_figures_not_the_models(
    seeded: None,
) -> None:
    account_id, symbol, _ = _a_holding()
    trades = [{"account_id": account_id, "action": "sell", "security": symbol, "amount": "2500"}]
    saved = _call(
        "save_recommendation",
        title="Trim",
        rationale="r",
        kind="trades",
        trades=trades,
        targets=TARGETS,
        estimated_tax_impact="12345.67",
    )
    assert saved["saved"] is True
    assert saved["is_feasible"] is True
    assert "estimated_tax_impact was ignored" in str(saved["notes"])

    record = _stored(str(saved["recommendation_id"]))
    simulated = _call("simulate_trades", trades=trades, targets=TARGETS)
    assert record.kind == "trades"  # type: ignore[attr-defined]
    assert record.estimated_tax_impact is None  # type: ignore[attr-defined]
    stored = record.simulation_json  # type: ignore[attr-defined]
    assert stored["trades"] == simulated["trades"]
    assert stored["tax"] == simulated["tax"]
    assert record.targets_json == TARGETS  # type: ignore[attr-defined]


def test_a_malformed_proposal_is_not_saved(seeded: None) -> None:
    no_trades = _call("save_recommendation", title="t", rationale="r", kind="trades")
    assert no_trades["saved"] is False
    bad_amount = _call(
        "save_recommendation",
        title="t",
        rationale="r",
        kind="trades",
        trades=[{"account_id": "x", "action": "sell", "security": "Y", "amount": 100}],
        targets=TARGETS,
    )
    assert bad_amount["saved"] is False
    no_targets = _call("save_recommendation", title="t", rationale="r", kind="target_change")
    assert no_targets["saved"] is False


def test_a_target_change_stores_the_gaps_it_would_open(seeded: None) -> None:
    saved = _call(
        "save_recommendation", title="Shift", rationale="r", kind="target_change", targets=TARGETS
    )
    assert saved["saved"] is True, saved
    record = _stored(str(saved["recommendation_id"]))
    assert record.kind == "target_change"  # type: ignore[attr-defined]
    assert record.targets_json == TARGETS  # type: ignore[attr-defined]
    assert record.simulation_json is not None  # type: ignore[attr-defined]


def test_a_recommendation_links_back_to_its_conversation(seeded: None) -> None:
    import uuid

    from portfolio_manager import database
    from portfolio_manager.models.advisory import AdvisorSession

    conversation = uuid.uuid4()
    with database.get_session_factory()() as setup:
        setup.add(AdvisorSession(id=conversation, hermes_session_id="h-1", title="chat"))
        setup.commit()

    saved = _call(
        "save_recommendation", title="t", rationale="r", conversation_id=str(conversation)
    )
    assert _stored(str(saved["recommendation_id"])).advisor_session_id == conversation  # type: ignore[attr-defined]

    unknown = _call("save_recommendation", title="t", rationale="r", conversation_id="nonsense")
    assert unknown["saved"] is True
    assert _stored(str(unknown["recommendation_id"])).advisor_session_id is None  # type: ignore[attr-defined]


# --- the HTTP transport ---------------------------------------------------


def test_the_mcp_endpoint_keeps_no_session_state() -> None:
    """A notification for a session the server no longer holds must not crash.

    Stateful mode let Hermes post to a reaped session: the 202 went out, the
    write to the closed session failed, and the library then tried to start a
    second response on the same request. Stateless mode has no session to lose.
    """
    from fastapi.testclient import TestClient

    from portfolio_manager.main import create_app

    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    with TestClient(create_app()) as client:
        first = client.post("/mcp/", json=init, headers=headers)
        assert first.status_code == 200
        assert "mcp-session-id" not in first.headers

        # A session id the server never issued, or one it has since dropped.
        stale = {**headers, "Mcp-Session-Id": "gone", "Mcp-Protocol-Version": "2025-06-18"}
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert client.post("/mcp/", json=note, headers=stale).status_code == 202

        listing = client.post(
            "/mcp/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=stale
        )
        assert listing.status_code == 200
        assert all(f'"{name}"' in listing.text for name in EXPOSED_TOOLS)
