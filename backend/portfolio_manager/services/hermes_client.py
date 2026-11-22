"""A thin client for Hermes's api_server, used by the Advisor page.

Hermes holds the conversation and does the reasoning and the research. This
module only carries requests to it and streams the answer back, so the browser
never needs the Hermes key and never talks to the agent directly.

Every failure to reach Hermes becomes `HermesUnavailableError`, with a message that
says where it looked. The rest of the application must not care whether Hermes
is running: that is the `HERMES_ENABLED=false` contract.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings


class HermesUnavailableError(RuntimeError):
    """Hermes is disabled, unreachable, or refused the request."""


@dataclass(frozen=True)
class SseEvent:
    event: str
    data: dict[str, Any]


@dataclass
class SseParser:
    """Incremental `text/event-stream` parsing, for watching a stream we relay.

    Only `event:` and `data:` lines matter here; comments (`: keepalive`) and
    `id:` lines are skipped. Data that is not JSON is kept as `{"raw": ...}`
    rather than dropped -- it is still something Hermes said.
    """

    _buffer: str = ""
    _event: str = "message"
    _data: list[str] = field(default_factory=list)

    def feed(self, chunk: bytes) -> Iterator[SseEvent]:
        self._buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line == "":
                if self._data:
                    yield self._emit()
                continue
            if line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "event":
                self._event = value
            elif name == "data":
                self._data.append(value)

    def _emit(self) -> SseEvent:
        text = "\n".join(self._data)
        event = self._event
        self._event, self._data = "message", []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = {"raw": text}
        return SseEvent(event=event, data=parsed if isinstance(parsed, dict) else {"value": parsed})


class HermesClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._transport = transport

    @property
    def base_url(self) -> str:
        return self._settings.hermes_api_url.rstrip("/")

    def _check_enabled(self) -> None:
        if not self._settings.hermes_enabled:
            raise HermesUnavailableError(
                "Hermes is disabled. Set HERMES_ENABLED=true and HERMES_API_KEY in .env, "
                "then run `portfolio hermes install` for the Hermes side."
            )

    def _client(self, *, read_timeout: float | None) -> httpx.AsyncClient:
        key = self._settings.hermes_api_key.get_secret_value()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        timeout = httpx.Timeout(
            connect=self._settings.hermes_connect_timeout_seconds,
            read=read_timeout,
            write=30.0,
            pool=5.0,
        )
        return httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=timeout, transport=self._transport
        )

    def _unreachable(self, exc: Exception) -> HermesUnavailableError:
        return HermesUnavailableError(
            f"Hermes is not reachable at {self.base_url} ({type(exc).__name__}). "
            "Is `hermes gateway` running with the api_server platform enabled?"
        )

    @staticmethod
    def _refused(response: httpx.Response) -> HermesUnavailableError:
        detail = response.text[:300]
        with contextlib.suppress(ValueError, AttributeError):
            detail = response.json().get("error", {}).get("message", detail)
        if response.status_code == 401:
            return HermesUnavailableError(
                "Hermes refused the API key. HERMES_API_KEY must match the api_server key."
            )
        return HermesUnavailableError(f"Hermes answered {response.status_code}: {detail}")

    async def _json(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self._check_enabled()
        try:
            async with self._client(read_timeout=60.0) as client:
                response = await client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if response.status_code >= 400:
            raise self._refused(response)
        return response.json()

    async def health(self) -> dict[str, Any]:
        result: dict[str, Any] = await self._json("GET", "/health")
        return result

    async def create_session(self, *, title: str, system_prompt: str) -> str:
        body = {"title": title, "system_prompt": system_prompt, "source": "portfolio-manager"}
        result = await self._json("POST", "/api/sessions", body)
        session_id = (result.get("session") or {}).get("id")
        if not session_id:
            raise HermesUnavailableError("Hermes created a session but did not return its id")
        return str(session_id)

    async def fork_session(self, hermes_session_id: str, *, title: str) -> str:
        result = await self._json(
            "POST", f"/api/sessions/{hermes_session_id}/fork", {"title": title}
        )
        session_id = (result.get("session") or {}).get("id")
        if not session_id:
            raise HermesUnavailableError("Hermes forked the session but did not return its id")
        return str(session_id)

    async def messages(self, hermes_session_id: str) -> list[dict[str, Any]]:
        result = await self._json("GET", f"/api/sessions/{hermes_session_id}/messages")
        data = result.get("data", result.get("messages", []))
        return list(data) if isinstance(data, list) else []

    async def open_stream(
        self, hermes_session_id: str, message: str
    ) -> tuple[httpx.AsyncClient, httpx.Response]:
        """Start a streamed turn. The caller owns closing both.

        Connecting is done eagerly, so an unreachable Hermes is a 503 for the
        request rather than an error buried inside a stream that already said 200.
        """
        self._check_enabled()
        client = self._client(read_timeout=None)
        try:
            request = client.build_request(
                "POST",
                f"/api/sessions/{hermes_session_id}/chat/stream",
                json={"message": message},
                headers={"Accept": "text/event-stream"},
            )
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise self._unreachable(exc) from exc
        if response.status_code >= 400:
            await response.aread()
            await response.aclose()
            await client.aclose()
            raise self._refused(response)
        return client, response


async def relay(
    client: httpx.AsyncClient, response: httpx.Response, parser: SseParser
) -> AsyncIterator[tuple[bytes, list[SseEvent]]]:
    """Yield each chunk unchanged, with the events completed by it."""
    try:
        async for chunk in response.aiter_bytes():
            yield chunk, list(parser.feed(chunk))
    finally:
        await response.aclose()
        await client.aclose()
