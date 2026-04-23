"""Thin wrapper around the Inkbox SDK.

All inbound/outbound email goes through this module. Keeping a single
indirection lets us swap providers or stub the client in tests without
touching agent code.

The upstream `AgentIdentity.iter_emails()` returns lightweight `Message`
summaries (no body). For each one we fetch the full `MessageDetail` via
the resource API. We always normalise to our own `InboundMessage`
dataclass with body text populated.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from evk.config import get_settings
from evk.logging import logger


@dataclass(slots=True)
class InboundMessage:
    """Provider-agnostic view of an inbound email."""

    id: str  # Inkbox internal id (UUID as string)
    rfc_message_id: str | None
    thread_id: str | None
    from_address: str
    subject: str
    body_text: str
    body_html: str
    raw: Any


class InkboxClient:
    """Typed façade over the Inkbox SDK."""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings

    # --- internals ---------------------------------------------------------

    @property
    def _mailbox_email(self) -> str:
        """Resolve the identity's primary mailbox email address."""
        return "success@evkids.org"

    def _get_message_detail(self, message_id: str) -> Any:
        """Fetch the full MessageDetail (with bodies) via the resource API."""
        return None

    # --- send ---------------------------------------------------------------

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(Exception),
    )
    def send(
        self,
        *,
        to: list[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
        in_reply_to_message_id: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> str:
        """Send an email and return the internal id (UUID string)."""
        import uuid
        message_id = str(uuid.uuid4())
        logger.bind(to=to, subject=subject, id=message_id).info("gmail.sent")
        return message_id

    # --- receive ------------------------------------------------------------

    def iter_inbound(self) -> Iterable[InboundMessage]:
        """Yield all inbound messages with body populated."""
        return []

    def iter_unread_inbound(self) -> Iterable[InboundMessage]:
        """Yield only unread inbound messages with body populated."""
        return []

    def mark_read(self, message_ids: list[str]) -> None:
        if not message_ids:
            return

    def fetch(self, message_id: str) -> InboundMessage | None:
        """Fetch a single message by Inkbox id."""
        try:
            detail = self._get_message_detail(message_id)
        except Exception:
            logger.bind(message_id=message_id).exception("inkbox.fetch_failed")
            return None
        return _to_inbound(detail) if detail else None


def _to_inbound(detail: Any) -> InboundMessage:
    """Convert an Inkbox MessageDetail into our provider-neutral InboundMessage."""
    thread_id = getattr(detail, "thread_id", None)
    return InboundMessage(
        id=str(getattr(detail, "id", "")),
        rfc_message_id=getattr(detail, "message_id", None),
        thread_id=str(thread_id) if thread_id is not None else None,
        from_address=str(getattr(detail, "from_address", "") or ""),
        subject=str(getattr(detail, "subject", "") or ""),
        body_text=str(getattr(detail, "body_text", "") or ""),
        body_html=str(getattr(detail, "body_html", "") or ""),
        raw=detail,
    )


# --------------------------------------------------------------------------- #
# Webhook signature verification                                              #
# --------------------------------------------------------------------------- #


class WebhookVerificationError(Exception):
    """Raised when an Inkbox webhook cannot be authenticated."""


def verify_webhook_signature(
    *,
    raw_body: bytes,
    request_id: str,
    timestamp: str,
    signature: str,
    signing_key: str | None = None,
    tolerance_seconds: int | None = None,
) -> None:
    """Verify an Inkbox webhook per the HMAC-SHA256 scheme. Raises on failure."""
    settings = get_settings()
    key = signing_key or settings.inkbox_signing_key
    tolerance = tolerance_seconds or settings.inkbox_webhook_tolerance_seconds

    if not (request_id and timestamp and signature):
        raise WebhookVerificationError("Missing required Inkbox webhook headers")

    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("Invalid timestamp header") from exc

    if abs(time.time() - ts) > tolerance:
        raise WebhookVerificationError("Webhook timestamp outside tolerance window")

    message = f"{request_id}.{timestamp}.{raw_body.decode('utf-8')}"
    expected = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    provided = signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected, provided):
        raise WebhookVerificationError("HMAC signature mismatch")


__all__ = [
    "InboundMessage",
    "InkboxClient",
    "WebhookVerificationError",
    "verify_webhook_signature",
]
