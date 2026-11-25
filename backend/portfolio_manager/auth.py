"""A single-user sign-in for when the app listens beyond this machine.

Off unless both `AUTH_USERNAME` and `AUTH_PASSWORD_HASH` are set. The server
refuses to bind a non-loopback address without them (see `serve.py`), because
everything here -- the portfolio, the advisor, the MCP tools -- is otherwise
open to anyone who can reach the port.

Deliberately small and dependency-free:

* **Password**: scrypt from the standard library, stored as
  `scrypt:<n>:<r>:<p>:<salt>:<hash>`. Colons rather than the usual `$`, because
  `.env` files go through shell-style `$VAR` interpolation in some tools.
* **Session**: an HMAC-signed cookie holding the username and an expiry. The key
  is derived from the stored password hash, so changing the password signs
  every existing session out, and there is no second secret to manage.
* **Exempt**: `/login`, `/logout`, `/health`, and `/mcp` *from loopback only* --
  Hermes runs on this machine and has no way to sign in through a form.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings, get_settings

COOKIE_NAME = "pm_session"

# scrypt cost: ~32 MiB and tens of milliseconds per check. Cheap for one
# person signing in, expensive for someone guessing.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024

#: Failed sign-ins allowed per client address inside the window.
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: Reachable without a session. Matched exactly.
PUBLIC_PATHS = frozenset({"/login", "/logout", "/health"})


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- passwords --------------------------------------------------------------


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("the password is empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt:{_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Whether `password` matches `stored`. A malformed hash never matches."""
    try:
        scheme, n, r, p, salt, expected = stored.split(":")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, _unb64(expected))


# --- sessions ---------------------------------------------------------------


def _session_key(settings: Settings) -> bytes:
    stored = settings.auth_password_hash.get_secret_value()
    return hashlib.sha256(b"pm-session-v1\0" + stored.encode()).digest()


def _sign(settings: Settings, payload: str) -> str:
    return _b64(hmac.new(_session_key(settings), payload.encode(), hashlib.sha256).digest())


def issue_session(settings: Settings, now: float | None = None) -> str:
    expires = int((now or time.time()) + settings.auth_session_days * 86400)
    payload = f"{_b64(settings.auth_username.encode())}.{expires}"
    return f"{payload}.{_sign(settings, payload)}"


def session_is_valid(settings: Settings, token: str | None, now: float | None = None) -> bool:
    if not token:
        return False
    try:
        user_b64, expires, signature = token.split(".")
        payload = f"{user_b64}.{expires}"
        if not hmac.compare_digest(signature, _sign(settings, payload)):
            return False
        if _unb64(user_b64).decode() != settings.auth_username:
            return False
        return int(expires) > (now or time.time())
    except (ValueError, UnicodeDecodeError):
        return False


# --- throttling -------------------------------------------------------------


class FailureThrottle:
    """Counts failed sign-ins per client address, in memory.

    Resets when the process restarts, which is acceptable for one user on a home
    network: the point is to make guessing slow, not to keep an audit trail.
    """

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, client: str, now: float) -> deque[float]:
        times = self._failures[client]
        while times and times[0] <= now - FAILURE_WINDOW_SECONDS:
            times.popleft()
        return times

    def blocked_for(self, client: str, now: float | None = None) -> int:
        """Seconds until `client` may try again; 0 if it may now."""
        now = now or time.time()
        times = self._prune(client, now)
        if len(times) < MAX_FAILURES:
            return 0
        return max(1, int(times[0] + FAILURE_WINDOW_SECONDS - now))

    def record_failure(self, client: str, now: float | None = None) -> None:
        now = now or time.time()
        self._prune(client, now).append(now)

    def clear(self, client: str) -> None:
        self._failures.pop(client, None)


# --- the page ---------------------------------------------------------------


def safe_next(target: str | None) -> str:
    """Where to send someone after signing in. Only a path on this site."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Sign in · Portfolio</title>
<style>
  :root {{ color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --ink:#111827;
          --ink2:#6b7280; --line:#e5e7eb; --accent:#111827; --accent-ink:#fff;
          --err:#b91c1c; --err-bg:#fef2f2; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0b0d10; --card:#14171c; --ink:#e5e7eb; --ink2:#9ca3af;
            --line:#262a31; --accent:#e5e7eb; --accent-ink:#0b0d10;
            --err:#fca5a5; --err-bg:#2a1414; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:var(--bg); color:var(--ink);
         font:15px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif; }}
  main {{ width:min(360px, calc(100vw - 32px)); background:var(--card);
         border:1px solid var(--line); border-radius:14px; padding:28px; }}
  h1 {{ margin:0 0 4px; font-size:18px; font-weight:600; letter-spacing:-0.01em; }}
  p.sub {{ margin:0 0 20px; color:var(--ink2); font-size:13px; }}
  label {{ display:block; font-size:13px; font-weight:500; margin:14px 0 6px; }}
  input {{ width:100%; padding:9px 11px; border-radius:8px; font:inherit;
          border:1px solid var(--line); background:transparent; color:inherit; }}
  input:focus {{ outline:2px solid var(--accent); outline-offset:1px; }}
  button {{ width:100%; margin-top:22px; padding:10px; border:0; border-radius:8px;
           background:var(--accent); color:var(--accent-ink); font:inherit;
           font-weight:600; cursor:pointer; }}
  .error {{ margin:0 0 4px; padding:9px 11px; border-radius:8px; font-size:13px;
           background:var(--err-bg); color:var(--err); }}
</style>
</head>
<body>
<main>
  <h1>Portfolio</h1>
  <p class="sub">Sign in to continue.</p>
  {error}
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{next}">
    <label for="username">Username</label>
    <input id="username" name="username" autocomplete="username" required autofocus
           value="{username}">
    <label for="password">Password</label>
    <input id="password" name="password" type="password"
           autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
</main>
</body>
</html>
"""


def render_login(next_path: str, error: str = "", username: str = "") -> str:
    return _PAGE.format(
        next=html.escape(safe_next(next_path), quote=True),
        username=html.escape(username, quote=True),
        error=f'<p class="error" role="alert">{html.escape(error)}</p>' if error else "",
    )


# --- middleware -------------------------------------------------------------


def _client_host(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else ""


def _cookie(scope: Scope, name: str) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return str(v)
    return None


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            return str(value.decode("latin-1"))
    return ""


class AuthMiddleware:
    """Requires a signed-in session for everything not explicitly exempt.

    Settings are resolved per request through `settings_provider`, so a test's
    `dependency_overrides[get_settings]` governs it the same way it governs the
    routes -- rather than the developer's own `.env` leaking into the suite.
    """

    def __init__(self, app: ASGIApp, settings_provider: Callable[[], Settings]) -> None:
        self.app = app
        self.settings_provider = settings_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = self.settings_provider()
        if not settings.auth_enabled or self._allowed(scope, settings):
            await self.app(scope, receive, send)
            return
        await self._refuse(scope, send)

    @staticmethod
    def _allowed(scope: Scope, settings: Settings) -> bool:
        path: str = scope["path"]
        if path in PUBLIC_PATHS:
            return True
        if (path == "/mcp" or path.startswith("/mcp/")) and _client_host(scope) in LOOPBACK_HOSTS:
            return True
        return session_is_valid(settings, _cookie(scope, COOKIE_NAME))

    @staticmethod
    async def _refuse(scope: Scope, send: Send) -> None:
        wants_page = scope["method"] == "GET" and "text/html" in _header(scope, b"accept")
        if wants_page:
            target = scope["path"]
            if scope.get("query_string"):
                target += "?" + scope["query_string"].decode("latin-1")
            headers = [
                (b"location", f"/login?next={quote(target, safe='')}".encode()),
                (b"cache-control", b"no-store"),
            ]
            await send({"type": "http.response.start", "status": 303, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return
        body = b'{"detail":"Not signed in"}'
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"x-auth-required", b"1"),
            (b"cache-control", b"no-store"),
        ]
        await send({"type": "http.response.start", "status": 401, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def settings_provider_for(app: FastAPI) -> Callable[[], Settings]:
    """Resolve settings the way a route's `SettingsDep` would."""

    def provide() -> Settings:
        override = app.dependency_overrides.get(get_settings, get_settings)
        result: Settings = override()
        return result

    return provide


def parse_form(body: bytes) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}


# --- routes -----------------------------------------------------------------


def install(app: FastAPI) -> None:
    """Add the sign-in routes and the middleware to `app`."""
    provide = settings_provider_for(app)
    throttle = FailureThrottle()

    def page(next_path: str, error: str = "", username: str = "", code: int = 200) -> Any:
        return HTMLResponse(
            render_login(next_path, error, username),
            status_code=code,
            headers={"cache-control": "no-store"},
        )

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request, next: str = "/") -> Any:
        settings = provide()
        if not settings.auth_enabled or session_is_valid(
            settings, request.cookies.get(COOKIE_NAME)
        ):
            return RedirectResponse(safe_next(next), status_code=303)
        return page(next)

    @app.post("/login", include_in_schema=False)
    async def login_submit(request: Request) -> Any:
        settings = provide()
        form = parse_form(await request.body())
        next_path = safe_next(form.get("next"))
        if not settings.auth_enabled:
            return RedirectResponse(next_path, status_code=303)

        client = request.client.host if request.client else ""
        username = form.get("username", "")
        wait = throttle.blocked_for(client)
        if wait:
            minutes = max(1, round(wait / 60))
            return page(
                next_path,
                f"Too many failed attempts. Try again in about {minutes} minute"
                f"{'' if minutes == 1 else 's'}.",
                username,
                code=429,
            )

        # Check the password even when the username is wrong, so the response
        # time does not say which of the two was.
        password_ok = verify_password(
            form.get("password", ""), settings.auth_password_hash.get_secret_value()
        )
        user_ok = hmac.compare_digest(username.encode(), settings.auth_username.encode())
        if not (password_ok and user_ok):
            throttle.record_failure(client)
            return page(next_path, "Wrong username or password.", username, code=401)

        throttle.clear(client)
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            issue_session(settings),
            max_age=settings.auth_session_days * 86400,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @app.post("/logout", include_in_schema=False)
    def logout() -> Any:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    app.add_middleware(AuthMiddleware, settings_provider=provide)
