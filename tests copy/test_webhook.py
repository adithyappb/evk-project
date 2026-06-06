"""HMAC webhook verification."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from evk.inkbox_client import WebhookVerificationError, verify_webhook_signature


def _sign(body: bytes, key: str, request_id: str, timestamp: str) -> str:
    message = f"{request_id}.{timestamp}.{body.decode('utf-8')}"
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


KEY = "whsec_test_signing_key"


def test_valid_signature_passes():
    body = b'{"event":"message.received","data":{"id":"msg_1"}}'
    ts = str(int(time.time()))
    rid = "req_123"
    sig = "sha256=" + _sign(body, KEY, rid, ts)
    # should not raise
    verify_webhook_signature(
        raw_body=body,
        request_id=rid,
        timestamp=ts,
        signature=sig,
        signing_key=KEY,
    )


def test_missing_headers_rejected():
    with pytest.raises(WebhookVerificationError, match="Missing"):
        verify_webhook_signature(
            raw_body=b"{}", request_id="", timestamp="", signature="", signing_key=KEY
        )


def test_stale_timestamp_rejected():
    body = b"{}"
    ts = str(int(time.time()) - 10_000)  # 10k seconds old
    rid = "req"
    sig = "sha256=" + _sign(body, KEY, rid, ts)
    with pytest.raises(WebhookVerificationError, match="tolerance"):
        verify_webhook_signature(
            raw_body=body, request_id=rid, timestamp=ts, signature=sig, signing_key=KEY
        )


def test_bad_signature_rejected():
    body = b"{}"
    ts = str(int(time.time()))
    rid = "req"
    bad_sig = "sha256=" + "0" * 64
    with pytest.raises(WebhookVerificationError, match="HMAC"):
        verify_webhook_signature(
            raw_body=body, request_id=rid, timestamp=ts, signature=bad_sig, signing_key=KEY
        )


def test_tampered_body_rejected():
    body = b'{"event":"message.received"}'
    ts = str(int(time.time()))
    rid = "req"
    sig = "sha256=" + _sign(body, KEY, rid, ts)
    # change the body after signing
    with pytest.raises(WebhookVerificationError):
        verify_webhook_signature(
            raw_body=b'{"event":"message.sent"}',
            request_id=rid,
            timestamp=ts,
            signature=sig,
            signing_key=KEY,
        )


def test_invalid_timestamp_format_rejected():
    with pytest.raises(WebhookVerificationError, match="Invalid timestamp"):
        verify_webhook_signature(
            raw_body=b"{}",
            request_id="rid",
            timestamp="not-a-number",
            signature="sha256=abc",
            signing_key=KEY,
        )
