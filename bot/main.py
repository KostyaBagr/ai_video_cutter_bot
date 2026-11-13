"""Entrypoint: long-polling Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import socket
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from bot.config import settings
from bot.db import init_db
from bot.handlers import router
from bot.middleware import DebugAccessMiddleware
from bot.video import require_ffmpeg, require_ffprobe

BOT_COMMANDS = [
    BotCommand(command="start", description="Начать / показать баланс"),
    BotCommand(command="balance", description="Баланс алмазов"),
    BotCommand(command="prices", description="Тарифы за действия"),
    BotCommand(command="shop", description="Купить алмазы"),
]


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
        force=True,
    )


def _telegram_session() -> AiohttpSession:
    """IPv4 + finite timeout.

    Docker's default bridge often has a blackhole IPv6 route. aiohttp then
    waits on api.telegram.org forever, so logs stop right after SQLite.
    """
    session = AiohttpSession(timeout=30.0)
    connector_init = getattr(session, "_connector_init", None)
    if isinstance(connector_init, dict):
        connector_init["family"] = socket.AF_INET
    return session


async def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)
    if not settings.telegram_bot_token:
        raise SystemExit("Задайте TELEGRAM_BOT_TOKEN в .env")
    if not settings.runware_api_key:
        raise SystemExit("Задайте RUNWARE_API_KEY в .env")
    if settings.is_debug and not settings.debug_telegram_id:
        raise SystemExit(
            "APP_ENV=debug, но не задан DEBUG_TELEGRAM_ID в .env"
        )
    require_ffmpeg()
    require_ffprobe()
    await init_db()

    log.info("Connecting to Telegram API (IPv4)…")
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=_telegram_session(),
    )
    try:
        me = await bot.get_me()
        log.info("Telegram OK: @%s (id=%s)", me.username, me.id)
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        log.exception(
            "Telegram API недоступен. Проверь сеть контейнера и TELEGRAM_BOT_TOKEN."
        )
        await bot.session.close()
        raise

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(DebugAccessMiddleware())
    dp.include_router(router)

    if settings.is_debug:
        log.info(
            "Bot polling in DEBUG (only telegram_id=%s)…",
            settings.debug_telegram_id,
        )
    else:
        log.info("Bot polling in PROD…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logging.getLogger(__name__).exception("Bot stopped")
        raise
