"""
TAKE coffee & more — Telegram bot.

Запуск (из корня проекта):
    python -m app.services.bot

Переменные окружения:
    BOT_TOKEN    — токен бота от @BotFather
    WEBAPP_URL   — https-адрес, на котором развёрнут templates/client.html
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
# httpx logs full request URLs at INFO, which includes the bot token
# (https://api.telegram.org/bot<TOKEN>/...) — keep it quiet enough not to
# leak that into `docker compose logs`.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("take_bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]

WELCOME_MESSAGE = (
    "Welcome to TAKE! ☕️\n\n"
    "Join our loyalty program right inside Telegram.\n\n"
    "💳 Collect points for every cup\n"
    "🎁 Get every 9th coffee for free\n"
    "✨ Track your balance instantly"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="Open TAKE Card", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )
    await update.message.reply_text(WELCOME_MESSAGE, reply_markup=keyboard)


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    return application


def main() -> None:
    application = build_application()
    logger.info("TAKE bot starting (polling)…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
