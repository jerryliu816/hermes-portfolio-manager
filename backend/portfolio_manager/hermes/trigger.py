"""Notifying Hermes that a new snapshot exists.

Strictly fire-and-forget. **Every failure is logged and swallowed**, because the
snapshot is already committed and valid by the time this runs: an analyst being
unreachable is not a reason to fail an import, and raising here would make the
portfolio's correctness depend on an optional component.

`NoOpAnalysisTrigger` is the default. The application must be fully usable with
Hermes absent, so the "not configured" path is the ordinary one rather than an
error case.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import httpx

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

SNAPSHOT_CREATED = "portfolio_snapshot_created"


@dataclass(frozen=True)
class TriggerResult:
    delivered: bool
    detail: str

    def __bool__(self) -> bool:
        return self.delivered


@runtime_checkable
class AnalysisTrigger(Protocol):
    def snapshot_created(self, snapshot_id: str, as_of_date: str) -> TriggerResult: ...


class NoOpAnalysisTrigger:
    """The default. Does nothing, successfully."""

    def snapshot_created(self, snapshot_id: str, as_of_date: str) -> TriggerResult:
        return TriggerResult(delivered=False, detail="hermes integration is disabled")


class HermesAnalysisTrigger:
    """Posts an HMAC-signed event to a Hermes webhook.

    The signature lets Hermes verify the event came from this system. The secret
    is the one `hermes webhook subscribe` prints when the subscription is
    created.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def configured(self) -> bool:
        return bool(
            self._settings.hermes_enabled
            and self._settings.hermes_webhook_url
            and self._settings.hermes_webhook_secret.get_secret_value()
        )

    def snapshot_created(self, snapshot_id: str, as_of_date: str) -> TriggerResult:
        if not self.configured:
            return TriggerResult(delivered=False, detail="hermes webhook is not configured")

        payload = {
            "event": SNAPSHOT_CREATED,
            "snapshot_id": snapshot_id,
            "as_of_date": as_of_date,
            "sent_at": datetime.now(UTC).isoformat(),
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        signature = sign(body, self._settings.hermes_webhook_secret.get_secret_value())

        try:
            response = httpx.post(
                self._settings.hermes_webhook_url,
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-portfolio-signature": signature,
                    "x-portfolio-event": SNAPSHOT_CREATED,
                },
                timeout=self._settings.hermes_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            # The snapshot is already valid. An unreachable analyst must never
            # turn a successful import into a failed one.
            logger.warning("hermes webhook could not be reached: %s", exc)
            return TriggerResult(delivered=False, detail=f"delivery failed: {exc}")

        if response.status_code >= 400:
            logger.warning(
                "hermes webhook returned %s for snapshot %s",
                response.status_code,
                snapshot_id,
            )
            return TriggerResult(delivered=False, detail=f"webhook returned {response.status_code}")
        return TriggerResult(delivered=True, detail=f"delivered ({response.status_code})")


def sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 of the exact bytes sent, hex encoded."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, secret: str, signature: str) -> bool:
    """Constant-time signature check, for a receiver to use."""
    return hmac.compare_digest(sign(body, secret), signature)


def get_trigger(settings: Settings | None = None) -> AnalysisTrigger:
    """The configured trigger, or the no-op one."""
    settings = settings or get_settings()
    if settings.hermes_enabled:
        return HermesAnalysisTrigger(settings)
    return NoOpAnalysisTrigger()
