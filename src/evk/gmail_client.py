"""Gmail IMAP + SMTP client using stdlib only (no Google API SDK required).

Uses an App Password for authentication — no OAuth needed. The interface
mirrors InkboxClient so the factory can swap them transparently.

Setup:
  1. Enable 2-Step Verification on the Gmail account.
  2. Go to myaccount.google.com → Security → App Passwords.
  3. Generate an App Password for "Mail".
  4. Set GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx in your .env.
"""

from __future__ import annotations

import email as email_lib
import imaplib
import smtplib
import ssl
import uuid
from collections.abc import Iterable
from email.header import decode_header as _decode_header
from email.message import EmailMessage, Message
from typing import Any

from evk.config import get_settings
from evk.inkbox_client import InboundMessage
from evk.logging import logger

_IMAP_HOST = "imap.gmail.com"
_IMAP_PORT = 993
_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


def _decode_mime_header(raw: str | bytes | None) -> str:
    """Decode a MIME-encoded header value into a plain Python string."""
    if raw is None:
        return ""
    parts = _decode_header(raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace"))
    decoded: list[str] = []
    for fragment, charset in parts:
        if isinstance(fragment, bytes):
            decoded.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(fragment)
    return " ".join(decoded)


def _extract_body(msg: Message) -> tuple[str, str]:
    """Walk a parsed email and return (plain_text, html_text)."""
    plain = ""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition") or "")
            if "attachment" in cd:
                continue
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(charset, errors="replace")
            if ct == "text/plain" and not plain:
                plain = text
            elif ct == "text/html" and not html:
                html = text
    else:
        ct = msg.get_content_type()
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        text = payload.decode(charset, errors="replace") if payload else ""
        if ct == "text/html":
            html = text
        else:
            plain = text
    return plain, html


class GmailEmailClient:
    """Production IMAP/SMTP client that authenticates with a Gmail App Password.

    Raises ``RuntimeError`` on construction if ``GMAIL_APP_PASSWORD`` is not set.
    """

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.gmail_app_password:
            raise RuntimeError(
                "GmailEmailClient requires GMAIL_APP_PASSWORD to be set. "
                "See src/evk/gmail_client.py for setup instructions."
            )
        self._user = settings.gmail_user
        self._password = settings.gmail_app_password
        self._imap_host = settings.gmail_imap_host
        self._imap_port = settings.gmail_imap_port
        self._settings = settings
        logger.bind(user=self._user).info("gmail_client.init")

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def iter_unread_inbound(self) -> Iterable[InboundMessage]:
        """Yield all UNSEEN messages from INBOX via IMAP."""
        ctx = ssl.create_default_context()
        logger.bind(host=self._imap_host, user=self._user).info("gmail.imap.connect")
        try:
            with imaplib.IMAP4_SSL(self._imap_host, self._imap_port, ssl_context=ctx) as imap:
                imap.login(self._user, self._password)
                imap.select("INBOX")
                _status, data = imap.search(None, "UNSEEN")
                if _status != "OK" or not data or not data[0]:
                    return
                uid_list = data[0].split()
                logger.bind(count=len(uid_list)).info("gmail.imap.unseen_found")
                for uid in uid_list:
                    uid_str = uid.decode("ascii")
                    _fs, msg_data = imap.fetch(uid, "(RFC822)")
                    if _fs != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw_bytes: bytes = msg_data[0][1]  # type: ignore[index]
                    parsed = email_lib.message_from_bytes(raw_bytes)
                    plain, html = _extract_body(parsed)
                    rfc_id = parsed.get("Message-ID")
                    references = parsed.get("References") or parsed.get("In-Reply-To")
                    thread_id = references.split()[0] if references else rfc_id
                    msg = InboundMessage(
                        id=uid_str,
                        rfc_message_id=rfc_id,
                        thread_id=thread_id,
                        from_address=_decode_mime_header(parsed.get("From")),
                        subject=_decode_mime_header(parsed.get("Subject")),
                        body_text=plain,
                        body_html=html,
                        raw=raw_bytes,
                    )
                    logger.bind(uid=uid_str, subject=msg.subject).debug("gmail.imap.message")
                    yield msg
        except imaplib.IMAP4.error as exc:
            logger.exception("gmail.imap.error")
            raise RuntimeError(f"IMAP error: {exc}") from exc

    def mark_read(self, message_ids: list[str]) -> None:
        """Set \\Seen flag on the given IMAP UIDs."""
        if not message_ids:
            return
        ctx = ssl.create_default_context()
        uid_str = ",".join(message_ids)
        try:
            with imaplib.IMAP4_SSL(self._imap_host, self._imap_port, ssl_context=ctx) as imap:
                imap.login(self._user, self._password)
                imap.select("INBOX")
                imap.store(uid_str, "+FLAGS", r"\Seen")
                logger.bind(uids=uid_str).info("gmail.imap.marked_read")
        except imaplib.IMAP4.error as exc:
            logger.exception("gmail.imap.mark_read_error")
            raise RuntimeError(f"IMAP mark_read error: {exc}") from exc

    def fetch_unread(self, limit: int = 10) -> list[InboundMessage]:
        """Convenience wrapper — returns up to *limit* unread messages as a list."""
        messages: list[InboundMessage] = []
        for msg in self.iter_unread_inbound():
            messages.append(msg)
            if len(messages) >= limit:
                break
        return messages

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

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
        """Send an email via Gmail SMTP (STARTTLS on port 587). Returns a message-id."""
        msg = EmailMessage()
        message_id = f"<{uuid.uuid4().hex}@evkids.gmail>"
        msg["From"] = self._user
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg["Message-ID"] = message_id
        if in_reply_to_message_id:
            msg["In-Reply-To"] = in_reply_to_message_id
            msg["References"] = in_reply_to_message_id
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
        try:
            with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as client:
                client.ehlo()
                client.starttls()
                client.ehlo()
                client.login(self._user, self._password)
                client.send_message(msg)
            logger.bind(to=to, subject=subject, id=message_id).info("gmail.smtp.sent")
        except smtplib.SMTPException as exc:
            logger.bind(to=to, subject=subject).exception("gmail.smtp.error")
            raise RuntimeError(f"SMTP send error: {exc}") from exc
        return message_id

    def send_verification(self, *, to: str, code: str, ttl_minutes: int) -> None:
        """Send a verification / OTP email via Gmail SMTP."""
        self.send(
            to=[to],
            subject="EVKids verification code",
            body_text=(
                f"Your EVKids verification code is {code}. "
                f"It expires in {ttl_minutes} minutes."
            ),
        )
        logger.bind(to=to).info("gmail.send_verification")


__all__ = ["GmailEmailClient"]
