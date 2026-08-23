"""
Security & authentication.

Two independent auth flows live here:

1. Telegram WebApp `initData` validation — verifies the HMAC-SHA256
   signature Telegram attaches to the launch payload, per the official spec:
   https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
   This proves the request genuinely came from Telegram and hasn't been
   tampered with, and lets us trust the embedded user info without asking
   the customer to log in.

2. Customer QR payload signing/verification — each customer's QR code
   encodes their telegram_id plus an HMAC token (signed with QR_SECRET_KEY,
   distinct from BOT_TOKEN) so a barista's scanner can trust the scanned
   code without a network round trip to Telegram, and so the payload can't
   be forged by editing the QR image.

3. Barista PIN session — a lightweight, per-process signed token so the
   barista doesn't have to enter the PIN before every single scan while a
   shift is running.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from app.config import settings


class InvalidInitData(Exception):
    """Raised when Telegram initData fails signature or freshness checks."""


class InvalidQRPayload(Exception):
    """Raised when a scanned customer QR payload fails signature checks."""


# ---------------------------------------------------------------------------
# 1. Telegram WebApp initData validation
# ---------------------------------------------------------------------------
def validate_telegram_init_data(init_data: str) -> dict:
    """
    Validate the raw `initData` string from `Telegram.WebApp.initData`.

    Returns the parsed data as a dict (with `user` already JSON-decoded)
    on success, or raises InvalidInitData on failure.
    """
    if settings.DEV_MODE:
        # Local development escape hatch: allows testing without a real
        # Telegram client. NEVER enabled in production (see .env.example).
        return {
            "user": {
                "id": 1,
                "first_name": "Dev",
                "username": "dev_user",
            },
            "auth_date": str(int(time.time())),
        }

    if not init_data:
        raise InvalidInitData("Empty initData")

    # initData is a URL-encoded query string, e.g.
    # "query_id=...&user=%7B...%7D&auth_date=...&hash=..."
    pairs = parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)

    received_hash = data.pop("hash", None)
    if not received_hash:
        raise InvalidInitData("Missing hash field")

    # Build the data-check-string: all key=value pairs (except `hash`),
    # sorted alphabetically by key, joined with \n.
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))

    # secret_key = HMAC_SHA256("WebAppData", bot_token)
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=settings.BOT_TOKEN.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    computed_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise InvalidInitData("Signature mismatch")

    # Freshness check — reject stale handshakes to prevent replay attacks.
    auth_date_raw = data.get("auth_date")
    if not auth_date_raw:
        raise InvalidInitData("Missing auth_date")

    try:
        auth_timestamp = int(auth_date_raw)
    except ValueError as exc:
        raise InvalidInitData("Malformed auth_date") from exc

    age_seconds = int(time.time()) - auth_timestamp
    if age_seconds > settings.INIT_DATA_MAX_AGE_SECONDS:
        raise InvalidInitData(
            f"initData is stale ({age_seconds}s old, "
            f"max {settings.INIT_DATA_MAX_AGE_SECONDS}s)"
        )
    if age_seconds < -60:  # allow small clock skew
        raise InvalidInitData("initData auth_date is in the future")

    user_raw = data.get("user")
    if not user_raw:
        raise InvalidInitData("Missing user field")

    try:
        data["user"] = json.loads(user_raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidInitData("Malformed user JSON") from exc

    return data


# ---------------------------------------------------------------------------
# 2. Customer QR payload signing / verification
# ---------------------------------------------------------------------------
def sign_qr_payload(telegram_id: int) -> str:
    """
    Build a compact, signed token for a customer's QR code:
        "<telegram_id>.<issued_at>.<hmac_hex>"

    The barista scanner verifies the signature locally (no DB round trip
    needed to decide whether to trust the code), then looks up the user by
    telegram_id.
    """
    issued_at = int(time.time())
    message = f"{telegram_id}.{issued_at}"
    signature = hmac.new(
        key=settings.QR_SECRET_KEY.encode("utf-8"),
        msg=message.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"{message}.{signature}"


def verify_qr_payload(payload: str) -> int:
    """
    Verify a scanned QR payload and return the embedded telegram_id.
    Raises InvalidQRPayload if the signature is invalid.

    Note: customer QR codes are intentionally long-lived (they represent
    "this is my loyalty card"), so unlike initData we do not enforce a
    short expiry here — only signature integrity.
    """
    parts = payload.strip().split(".")
    if len(parts) != 3:
        raise InvalidQRPayload("Malformed QR payload")

    telegram_id_str, issued_at_str, signature = parts
    message = f"{telegram_id_str}.{issued_at_str}"

    expected_signature = hmac.new(
        key=settings.QR_SECRET_KEY.encode("utf-8"),
        msg=message.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, signature):
        raise InvalidQRPayload("Signature mismatch")

    try:
        return int(telegram_id_str)
    except ValueError as exc:
        raise InvalidQRPayload("Malformed telegram_id in payload") from exc


# ---------------------------------------------------------------------------
# 3. Barista PIN session token
# ---------------------------------------------------------------------------
def check_barista_pin(pin: str) -> bool:
    return hmac.compare_digest(pin.strip(), settings.BARISTA_PIN)


def issue_barista_session_token() -> str:
    """
    Issue a signed session token after successful PIN entry, so the barista
    UI can store it (e.g. in memory / sessionStorage on the device) and
    attach it as a bearer token to subsequent scan/earn/redeem calls
    without re-entering the PIN on every transaction.
    """
    issued_at = int(time.time())
    message = f"barista.{issued_at}"
    signature = hmac.new(
        key=settings.QR_SECRET_KEY.encode("utf-8"),
        msg=message.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"{message}.{signature}"


def verify_barista_session_token(token: str, max_age_seconds: int = 12 * 3600) -> bool:
    """Verify a barista session token issued by issue_barista_session_token."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        return False

    prefix, issued_at_str, signature = parts
    if prefix != "barista":
        return False

    message = f"{prefix}.{issued_at_str}"
    expected_signature = hmac.new(
        key=settings.QR_SECRET_KEY.encode("utf-8"),
        msg=message.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, signature):
        return False

    try:
        issued_at = int(issued_at_str)
    except ValueError:
        return False

    age = int(time.time()) - issued_at
    return 0 <= age <= max_age_seconds
