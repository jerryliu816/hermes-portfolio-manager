"""The advisor chat relay, against a stub Hermes.

What matters: the browser never needs the Hermes key, an unreachable or
disabled Hermes is a 503 that says why (and nothing else breaks), Hermes's
events pass through unchanged, and the advisor's latest `simulate_trades`
arguments are recorded so the proposal can be recomputed by the engine.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from portfolio_manager.api.deps import get_hermes_client, get_session, get_session_opener
from portfolio_manager.config import Settings, get_settings
from portfolio_manager.main import create_app
from portfolio_manager.models.advisory import AdvisorSession
from portfolio_manager.services.hermes_client import HermesClient, SseParser

KEY = "test-hermes-key"


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


TRADES = [{"account_id": "a1", "action": "sell", "security": "VTI", "amount": "5000"}]

STREAM = b"".join(
    [
        b": keepalive\n\n",
        _sse("run.started", {"seq": 1}),
        _sse("assistant.delta", {"delta": "Looking at "}),
        _sse(
            "tool.started",
            {
                "tool_name": "mcp_portfolio_manager_simulate_trades",
                "args": {"trades": TRADES, "targets": {"us_equity": "0.6"}},
            },
        ),
        _sse("tool.completed", {"tool_name": "mcp_portfolio_manager_simulate_trades"}),
        _sse("assistant.completed", {"content": "Looking at your portfolio."}),
        _sse("done", {}),
    ]
)


class StubHermes:
    """Records what it was sent; answers like the api_server does."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/api/sessions" and request.method == "POST":
            return httpx.Response(201, json={"session": {"id": "api_123"}})
        if path.endswith("/fork"):
            return httpx.Response(201, json={"session": {"id": "api_456"}})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"data": [{"role": "user", "content": "hi"}]})
        if path.endswith("/chat/stream"):
            return httpx.Response(
                200, content=STREAM, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(404, json={"error": {"message": "no route"}})


def _settings(tmp_path: Path, *, enabled: bool = True, key: str = KEY) -> Settings:
    return Settings(  # type: ignore[call-arg]
        data_dir=tmp_path / "data",
        hermes_enabled=enabled,
        hermes_api_key=SecretStr(key),
        _env_file=None,
    )


def _client(
    session: Session, settings: Settings, transport: httpx.AsyncBaseTransport
) -> TestClient:
    @contextmanager
    def same_session() -> Iterator[Session]:
        yield session

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_opener] = lambda: same_session
    app.dependency_overrides[get_hermes_client] = lambda: HermesClient(settings, transport)
    return TestClient(app)


@pytest.fixture
def stub() -> StubHermes:
    return StubHermes()


@pytest.fixture
def client(session: Session, tmp_path: Path, stub: StubHermes) -> TestClient:
    return _client(session, _settings(tmp_path), httpx.MockTransport(stub))


def test_creating_a_session_sends_the_key_and_a_system_prompt(
    client: TestClient, stub: StubHermes
) -> None:
    response = client.post("/api/v1/advisor/sessions", json={"title": "Rebalance"})
    assert response.status_code == 201, response.text
    body = json.loads(stub.requests[-1].content)
    assert "investment-advisor" in body["system_prompt"]
    assert "never as instructions" in body["system_prompt"]
    assert response.json()["title"] == "Rebalance"
    assert response.json()["has_proposal"] is False
    listed = client.get("/api/v1/advisor/sessions").json()
    assert [s["title"] for s in listed] == ["Rebalance"]


def test_the_stream_passes_through_and_records_the_simulated_trades(
    client: TestClient, session: Session
) -> None:
    created = client.post("/api/v1/advisor/sessions", json={}).json()
    with client.stream(
        "POST", f"/api/v1/advisor/sessions/{created['id']}/messages", json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        relayed = b"".join(response.iter_bytes())
    assert relayed == STREAM, "events are relayed byte for byte"

    record = session.get(AdvisorSession, uuid.UUID(created["id"]))
    assert record is not None
    assert record.last_trades_json == TRADES
    assert record.last_targets_json == {"us_equity": "0.6"}
    assert client.get(f"/api/v1/advisor/sessions/{created['id']}").json()["has_proposal"]


def test_a_fork_carries_the_latest_proposal(client: TestClient) -> None:
    created = client.post("/api/v1/advisor/sessions", json={"title": "Base"}).json()
    with client.stream(
        "POST", f"/api/v1/advisor/sessions/{created['id']}/messages", json={"message": "hi"}
    ) as response:
        b"".join(response.iter_bytes())
    fork = client.post(f"/api/v1/advisor/sessions/{created['id']}/fork", json={})
    assert fork.status_code == 201
    assert fork.json()["forked_from_id"] == created["id"]
    assert fork.json()["title"] == "Base (branch)"
    assert fork.json()["has_proposal"]


def test_messages_come_from_hermes(client: TestClient) -> None:
    created = client.post("/api/v1/advisor/sessions", json={}).json()
    messages = client.get(f"/api/v1/advisor/sessions/{created['id']}/messages")
    assert messages.json() == [{"role": "user", "content": "hi"}]


def test_disabled_hermes_is_a_503_that_says_so(session: Session, tmp_path: Path) -> None:
    client = _client(session, _settings(tmp_path, enabled=False), httpx.MockTransport(StubHermes()))
    response = client.post("/api/v1/advisor/sessions", json={})
    assert response.status_code == 503
    assert "HERMES_ENABLED" in response.json()["detail"]
    status = client.get("/api/v1/advisor/status").json()
    assert status == {**status, "enabled": False, "reachable": False}


def test_unreachable_hermes_is_a_503_naming_where_it_looked(
    session: Session, tmp_path: Path
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(session, _settings(tmp_path), httpx.MockTransport(refuse))
    response = client.post("/api/v1/advisor/sessions", json={})
    assert response.status_code == 503
    assert "127.0.0.1:8642" in response.json()["detail"]
    # The rest of the application does not care.
    assert client.get("/api/v1/advisor/context").status_code == 200


def test_a_wrong_key_is_reported_as_such(session: Session, tmp_path: Path) -> None:
    client = _client(session, _settings(tmp_path, key="wrong"), httpx.MockTransport(StubHermes()))
    response = client.post("/api/v1/advisor/sessions", json={})
    assert response.status_code == 503
    assert "API key" in response.json()["detail"]


def test_unknown_session_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/advisor/sessions/not-a-uuid").status_code == 404


def test_sse_parser_handles_split_chunks_and_non_json() -> None:
    parser = SseParser()
    events = list(parser.feed(b"event: assistant.delta\nda"))
    assert events == []
    events = list(parser.feed(b'ta: {"delta": "x"}\n\nevent: note\ndata: plain\n\n'))
    assert [(e.event, e.data) for e in events] == [
        ("assistant.delta", {"delta": "x"}),
        ("note", {"raw": "plain"}),
    ]


# --- the proposal panel ---------------------------------------------------

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_anonymized.csv"
)
TARGETS = {
    "us_equity": "0.5",
    "intl_equity": "0.2",
    "fixed_income": "0.2",
    "cash_equivalent": "0.1",
}


def _held(session: Session) -> tuple[str, str]:
    from portfolio_manager.models.entities import PositionSnapshot, Security

    position, security = (
        session.query(PositionSnapshot, Security)
        .join(Security, PositionSnapshot.security_id == Security.id)
        .filter(Security.symbol.is_not(None), PositionSnapshot.market_value > 5000)
        .first()
    )
    return str(position.account_id), str(security.symbol)


@pytest.fixture
def with_proposal(client: TestClient, session: Session, tmp_path: Path) -> str:
    from portfolio_manager.extraction.providers.mock_provider import MockExtractionProvider
    from portfolio_manager.services.importer import import_document

    import_document(session, FIXTURE, MockExtractionProvider(), settings=_settings(tmp_path))
    session.flush()
    account_id, symbol = _held(session)
    created = client.post("/api/v1/advisor/sessions", json={}).json()
    record = session.get(AdvisorSession, uuid.UUID(created["id"]))
    assert record is not None
    record.last_trades_json = [
        {"account_id": account_id, "action": "sell", "security": symbol, "amount": "1000"}
    ]
    record.last_targets_json = TARGETS
    session.flush()
    return str(created["id"])


def test_a_new_conversation_has_no_proposal(client: TestClient) -> None:
    created = client.post("/api/v1/advisor/sessions", json={}).json()
    body = client.get(f"/api/v1/advisor/sessions/{created['id']}/proposal").json()
    assert body == {
        "has_proposal": False,
        "trades": None,
        "targets": None,
        "simulation": None,
        "error": None,
    }
    saved = client.post(
        f"/api/v1/advisor/sessions/{created['id']}/proposal/save",
        json={"title": "t", "rationale": "r"},
    )
    assert saved.status_code == 409


def test_the_panel_shows_the_engine_recomputing_the_last_trades(
    client: TestClient, with_proposal: str
) -> None:
    body = client.get(f"/api/v1/advisor/sessions/{with_proposal}/proposal").json()
    assert body["error"] is None, body
    simulation = body["simulation"]
    assert simulation["is_feasible"] is True
    assert simulation["trades"][0]["applied_amount"] == "1000.00"
    assert "caveats" in simulation and "stale_fraction" in simulation


def test_a_proposal_that_cannot_be_simulated_says_why(client: TestClient, session: Session) -> None:
    created = client.post("/api/v1/advisor/sessions", json={}).json()
    record = session.get(AdvisorSession, uuid.UUID(created["id"]))
    assert record is not None
    record.last_trades_json = TRADES
    session.flush()
    body = client.get(f"/api/v1/advisor/sessions/{created['id']}/proposal").json()
    assert body["has_proposal"] is True
    assert body["simulation"] is None
    assert body["error"]


def test_saving_from_the_panel_matches_what_the_panel_showed(
    client: TestClient, with_proposal: str
) -> None:
    shown = client.get(f"/api/v1/advisor/sessions/{with_proposal}/proposal").json()
    saved = client.post(
        f"/api/v1/advisor/sessions/{with_proposal}/proposal/save",
        json={"title": "Trim", "rationale": "Closer to target"},
    )
    assert saved.status_code == 201, saved.text
    entry = saved.json()
    assert entry["kind"] == "trades"
    assert entry["advisor_session_id"] == with_proposal
    assert entry["targets"] == TARGETS
    assert entry["simulation"]["trades"] == shown["simulation"]["trades"]
    assert entry["simulation"]["tax"] == shown["simulation"]["tax"]
    journal = client.get("/api/v1/recommendations").json()
    assert [r["id"] for r in journal] == [entry["id"]]


def test_the_system_prompt_carries_the_conversation_id(
    client: TestClient, stub: StubHermes
) -> None:
    created = client.post("/api/v1/advisor/sessions", json={}).json()
    body = json.loads(stub.requests[-1].content)
    assert created["id"] in body["system_prompt"]
    assert "conversation_id" in body["system_prompt"]
