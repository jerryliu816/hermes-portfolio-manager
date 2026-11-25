"""Run the API on the address the settings name, refusing an unsafe one.

`scripts/serve.sh` uses this rather than calling uvicorn with a hard-coded
host, so `API_HOST` in `.env` decides where the service listens. Binding beyond
loopback without the sign-in configured is refused outright: nothing else stands
between the network and the portfolio, the advisor, and the MCP tools.
"""

from __future__ import annotations

import sys

from .config import Settings, get_settings


def check_bind(settings: Settings) -> str | None:
    """Why `settings` must not be served, or None if it may."""
    if settings.binds_loopback_only or settings.auth_enabled:
        return None
    return (
        f"refusing to listen on {settings.api_host}: the app would be reachable from the "
        "network with no sign-in. Run `portfolio auth set-password` first, or set "
        "API_HOST=127.0.0.1."
    )


def main() -> None:
    settings = get_settings()
    problem = check_bind(settings)
    if problem:
        print(problem, file=sys.stderr)
        raise SystemExit(2)

    import uvicorn

    uvicorn.run("portfolio_manager.main:app", host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
