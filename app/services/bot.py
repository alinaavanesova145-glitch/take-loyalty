"""
Telegram Bot API notification service.

Sends the customer a push notification via the Bot API's `sendMessage`
endpoint immediately after a successful EARN or REDEEM transaction. This
is fire-and-forget from the caller's perspective — a failure to notify
must never roll back or block the transaction itself, since the bonus
balance update already succeeded and is the source of truth.
"""
import logging

import httpx

from app.config import settings
from app.models import TransactionType

logger = logging.getLogger("take_loyalty.bot")

TELEGRAM_API_BASE = "https://api.telegram.org"


def _build_message(
    *,
    transaction_type: TransactionType,
    bonus_change: int,
    branch_name: str,
    new_balance: int,
) -> str:
    if transaction_type == TransactionType.EARN:
        return (
            f"✨ Transaction Successful! You earned +{bonus_change} ֏ bonuses "
            f"at {branch_name}. New Balance: {new_balance} ֏."
        )
    # REDEEM
    return (
        f"✅ Bonus Redeemed! You used {abs(bonus_change)} ֏ bonuses at "
        f"{branch_name}. New Balance: {new_balance} ֏."
    )


async def send_transaction_notification(
    *,
    telegram_id: int,
    transaction_type: TransactionType,
    bonus_change: int,
    branch_name: str,
    new_balance: int,
) -> bool:
    """
    Sends a transaction notification to the customer's Telegram chat.

    Returns True on success, False on any failure (network error, bot
    blocked by user, etc). Never raises — callers should not have to
    handle notification failures as request-breaking errors.
    """
    text = _build_message(
        transaction_type=transaction_type,
        bonus_change=bonus_change,
        branch_name=branch_name,
        new_balance=new_balance,
    )

    url = f"{TELEGRAM_API_BASE}/bot{settings.BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": telegram_id,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
        if response.status_code != 200:
            logger.warning(
                "Telegram sendMessage failed (status=%s, chat_id=%s): %s",
                response.status_code,
                telegram_id,
                response.text,
            )
            return False
        return True
    except httpx.HTTPError as exc:
        logger.warning("Telegram sendMessage network error (chat_id=%s): %s", telegram_id, exc)
        return False


async def set_webapp_menu_button() -> bool:
    """
    Optional helper: configures the bot's persistent menu button to open
    the loyalty WebApp directly (`setChatMenuButton`). Safe to call once
    during deployment/setup — see README for a one-off invocation example.
    """
    url = f"{TELEGRAM_API_BASE}/bot{settings.BOT_TOKEN}/setChatMenuButton"
    payload = {
        "menu_button": {
            "type": "web_app",
            "text": "Open Loyalty Card",
            "web_app": {"url": f"{settings.BASE_URL}/app"},
        }
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
        return response.status_code == 200
    except httpx.HTTPError as exc:
        logger.warning("setChatMenuButton failed: %s", exc)
        return False
