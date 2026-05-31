"""Twilio client — optional WhatsApp and SMS delivery for student notifications.

Setup:
  1. Create a Twilio account at twilio.com (free trial available).
  2. Run: uv add twilio
  3. Add to .env:
       TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxx
       TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxx
       TWILIO_FROM_NUMBER=+15551234567           # for SMS
       TWILIO_WHATSAPP_FROM=whatsapp:+14155238886  # for WhatsApp (sandbox number)
  4. For WhatsApp production, request a business number at twilio.com/whatsapp.

This module never raises on import — the Twilio SDK is only loaded lazily when
TwilioClient() is constructed, so the rest of the app works without `twilio` installed.
"""

from __future__ import annotations

from evk.config import get_settings
from evk.logging import logger


class TwilioClient:
    """Thin wrapper around the Twilio REST client for WhatsApp + SMS."""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.twilio_account_sid or not settings.twilio_auth_token:
            raise RuntimeError(
                "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in .env. "
                "Also run: uv add twilio"
            )
        try:
            from twilio.rest import Client  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "Twilio SDK not installed — run: uv add twilio"
            ) from exc

        self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        self._settings = settings

    def send_whatsapp(self, *, to: str, body: str) -> str:
        """Send a WhatsApp message. Returns the Twilio message SID."""
        to_addr = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
        msg = self._client.messages.create(
            from_=self._settings.twilio_whatsapp_from,
            to=to_addr,
            body=body[:4096],
        )
        logger.bind(sid=msg.sid, to=to_addr).info("twilio.whatsapp_sent")
        return str(msg.sid)

    def send_sms(self, *, to: str, body: str) -> str:
        """Send an SMS. `to` must be E.164 format, e.g. +447911123456."""
        msg = self._client.messages.create(
            from_=self._settings.twilio_from_number,
            to=to,
            body=body[:1600],
        )
        logger.bind(sid=msg.sid, to=to).info("twilio.sms_sent")
        return str(msg.sid)


def is_twilio_configured() -> bool:
    """Return True if Twilio credentials are present in settings."""
    s = get_settings()
    return bool(s.twilio_account_sid and s.twilio_auth_token)


__all__ = ["TwilioClient", "is_twilio_configured"]
