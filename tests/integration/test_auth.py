"""The sign-in that guards the app once it listens beyond loopback."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portfolio_manager import auth
from portfolio_manager.config import Settings, get_settings
from portfolio_manager.main import create_app
from portfolio_manager.serve import check_bind

USERNAME = "owner"
PASSWORD = "correct horse battery staple"  # noqa: S105 - test credential


@pytest.fixture(scope="module")
def password_hash() -> str:
    return auth.hash_password(PASSWORD)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(data_dir=tmp_path / "data", _env_file=None, **overrides)  # type: ignore[arg-type,call-arg]


@pytest.fixture
def protected(tmp_path: Path, password_hash: str) -> Iterator[TestClient]:
    settings = _settings(tmp_path, auth_username=USERNAME, auth_password_hash=password_hash)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, follow_redirects=False) as client:
        yield client


def _sign_in(client: TestClient, password: str = PASSWORD, next_path: str = "/") -> object:
    return client.post(
        "/login", data={"username": USERNAME, "password": password, "next": next_path}
    )


# --- passwords and sessions -------------------------------------------------


def test_a_password_verifies_against_its_own_hash_only(password_hash: str) -> None:
    assert auth.verify_password(PASSWORD, password_hash)
    assert not auth.verify_password(PASSWORD + "x", password_hash)
    assert not auth.verify_password(PASSWORD, "not-a-hash")
    assert "$" not in password_hash, "a $ in .env is subject to interpolation"


def test_a_session_is_rejected_once_tampered_with_or_expired(
    tmp_path: Path, password_hash: str
) -> None:
    settings = _settings(tmp_path, auth_username=USERNAME, auth_password_hash=password_hash)
    token = auth.issue_session(settings, now=1_000.0)
    assert auth.session_is_valid(settings, token, now=1_001.0)
    assert not auth.session_is_valid(settings, token, now=1_000.0 + 31 * 86400)
    user, expires, sig = token.split(".")
    assert not auth.session_is_valid(settings, f"{user}.{int(expires) + 999}.{sig}", now=1_001.0)


def test_changing_the_password_signs_existing_sessions_out(
    tmp_path: Path, password_hash: str
) -> None:
    before = _settings(tmp_path, auth_username=USERNAME, auth_password_hash=password_hash)
    token = auth.issue_session(before)
    after = _settings(
        tmp_path, auth_username=USERNAME, auth_password_hash=auth.hash_password("another one")
    )
    assert not auth.session_is_valid(after, token)


# --- the middleware ---------------------------------------------------------


def test_off_by_default(tmp_path: Path) -> None:
    app = create_app()
    settings = _settings(tmp_path)
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    assert client.get("/health").json()["auth_enabled"] is False
    assert client.get("/openapi.json").status_code == 200


def test_the_api_answers_401_until_signed_in(protected: TestClient) -> None:
    refused = protected.get("/api/v1/portfolio/summary")
    assert refused.status_code == 401
    assert refused.headers["x-auth-required"] == "1"

    assert _sign_in(protected).status_code == 303
    assert protected.get("/openapi.json").status_code == 200


def test_a_browser_is_sent_to_the_sign_in_page_and_back(protected: TestClient) -> None:
    refused = protected.get("/holdings?sort=value", headers={"accept": "text/html"})
    assert refused.status_code == 303
    assert refused.headers["location"] == "/login?next=%2Fholdings%3Fsort%3Dvalue"

    page = protected.get("/login?next=/holdings")
    assert page.status_code == 200
    assert 'name="password"' in page.text

    signed_in = _sign_in(protected, next_path="/holdings")
    assert signed_in.headers["location"] == "/holdings"
    cookie = signed_in.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_a_wrong_password_is_refused_and_sets_no_cookie(protected: TestClient) -> None:
    response = _sign_in(protected, password="wrong")
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert protected.get("/openapi.json").status_code == 401


def test_repeated_failures_are_throttled(protected: TestClient) -> None:
    for _ in range(auth.MAX_FAILURES):
        assert _sign_in(protected, password="wrong").status_code == 401
    # Even the right password waits out the window once it is reached.
    assert _sign_in(protected).status_code == 429


@pytest.mark.parametrize("target", ["https://elsewhere.example", "//elsewhere.example", "x"])
def test_next_cannot_leave_the_site(protected: TestClient, target: str) -> None:
    assert _sign_in(protected, next_path=target).headers["location"] == "/"


def test_health_and_sign_out_stay_reachable(protected: TestClient) -> None:
    assert protected.get("/health").status_code == 200
    _sign_in(protected)
    out = protected.post("/logout")
    assert out.status_code == 303
    assert protected.get("/openapi.json").status_code == 401


def test_mcp_is_open_to_loopback_only(tmp_path: Path, password_hash: str) -> None:
    """Hermes runs on this machine and cannot fill in a form."""
    settings = _settings(tmp_path, auth_username=USERNAME, auth_password_hash=password_hash)
    for host, expected_open in [("127.0.0.1", True), ("192.168.1.50", False)]:
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings
        with TestClient(app, follow_redirects=False, client=(host, 50000)) as client:
            status = client.post("/mcp/", json={}).status_code
        assert (status != 401) is expected_open, (host, status)


# --- the bind guard ---------------------------------------------------------


def test_serving_on_the_network_requires_a_sign_in(tmp_path: Path, password_hash: str) -> None:
    assert check_bind(_settings(tmp_path)) is None
    assert check_bind(_settings(tmp_path, api_host="0.0.0.0")) is not None  # noqa: S104
    assert (
        check_bind(
            _settings(
                tmp_path,
                api_host="0.0.0.0",  # noqa: S104
                auth_username=USERNAME,
                auth_password_hash=password_hash,
            )
        )
        is None
    )
