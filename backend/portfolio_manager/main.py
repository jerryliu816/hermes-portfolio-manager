"""The FastAPI application.

The API and the built frontend are served from one origin, which removes an
entire class of CORS and cross-site-cookie problems and means the whole app is
one container.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from .api.deps import SettingsDep
from .api.v1 import api_router
from .config import get_settings
from .hermes.mcp_server import build_mcp_server

DESCRIPTION = """\
A personal portfolio system of record.

Two conventions run through every response and are worth knowing up front:

* **Money is a string, never a JSON number.** A JSON number is a float in most
  clients, and a float cannot represent money exactly. Format the string for
  display; do not do arithmetic on it.
* **Figures travel with their caveats.** Totals are accompanied by how much of
  the underlying data is stale, unclassified, or missing a cost basis, because
  a bare number would be more confident than the data behind it.

Account identifiers are masked: a real brokerage account number is never
returned. Clients address accounts by this system's own UUID.
"""


#: Paths the server answers itself. The frontend router owns everything else,
#: so these must never fall back to the single-page app.
RESERVED_PREFIXES = ("api/", "mcp", "health", "docs", "redoc", "openapi.json")


class SinglePageApp(StaticFiles):
    """Static files, with unmatched paths falling back to `index.html`.

    The router lives in the browser, so `/holdings` is a real page but not a
    real file. Plain `StaticFiles` 404s it, which means the app works while you
    click through it and breaks the moment you reload, bookmark, or follow a
    link straight to a page -- including the deep links this app prints.

    Only the document request falls back. A missing asset still 404s, because
    answering a request for a stylesheet with HTML turns a cache-busting
    mistake into a blank screen with no error.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            # Starlette signals a miss by raising, not by returning a 404, so
            # inspecting the response status would never see one.
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            # A missing *asset* stays a 404. Answering a request for a script
            # with HTML turns a stale filename into a blank screen and a
            # confusing console error instead of a clear miss.
            if Path(path).suffix:
                raise
            # So does anything addressed to the server rather than the router.
            # A mistyped endpoint must answer 404, not 200 and a web page: a
            # client parsing that gets a syntax error instead of a status it
            # can act on.
            if path.startswith(RESERVED_PREFIXES):
                raise
            return await super().get_response("index.html", scope)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Portfolio Manager",
        description=DESCRIPTION,
        version="0.1.0",
        openapi_tags=[
            {"name": "portfolio", "description": "Summary, allocation, concentration, gains"},
            {"name": "accounts", "description": "Accounts, classification, source precedence"},
            {"name": "holdings", "description": "Holdings and snapshot comparison"},
            {"name": "imports", "description": "Document upload and extraction"},
        ],
    )
    app.include_router(api_router)

    # Hermes reads the portfolio over MCP at /mcp. Mounted into the same app so
    # there is one process and one port; the Hermes client uses Streamable HTTP
    # for `url` servers, which is what `http_app()` serves.
    #
    # Stateless: every request gets its own transport. In stateful mode a
    # long-lived session can be reaped (idle timeout) while Hermes still holds
    # its id; a notification posted to it is acknowledged with 202, the write
    # to the closed session then fails, and the MCP library tries to send a 500
    # on a response that is already complete ("Unexpected ASGI message
    # 'http.response.start' sent, after response already completed"). No tool
    # here needs session state -- no sampling, elicitation or progress -- so
    # there is nothing to lose by not keeping any.
    mcp_app = build_mcp_server().http_app(path="/", transport="http", stateless_http=True)

    # A Starlette mount matches only paths *below* it, so bare `/mcp` would 405
    # -- and `/mcp` is what anyone registering the server types. A 307 keeps the
    # method and body intact, which a JSON-RPC POST depends on.
    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
    def mcp_root() -> RedirectResponse:
        return RedirectResponse("/mcp/", status_code=307)

    app.mount("/mcp", mcp_app)

    # FastMCP's session manager needs its lifespan to run, and mounting alone
    # does not chain it. Without this the first tool call fails at runtime.
    app.router.lifespan_context = mcp_app.lifespan

    # Settings come through the dependency, like every API route, so what this
    # reports is what the request would actually use -- and a test override of
    # the settings reaches it rather than the developer's own .env.
    @app.get("/health", tags=["ops"])
    def health(request_settings: SettingsDep) -> dict[str, Any]:
        return {"status": "ok", "provider": request_settings.extraction_provider}

    # Serve the built frontend when it exists, so production is one origin.
    # Mounted last so it never shadows an API route.
    if settings.frontend_dir.is_dir():
        app.mount("/", SinglePageApp(directory=settings.frontend_dir, html=True), name="frontend")

    return app


app = create_app()
