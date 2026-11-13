"""Entrypoint: long-polling Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
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
    )


async def main() -> None:
    setup_logging()
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

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await bot.set_my_commands(BOT_COMMANDS)

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(DebugAccessMiddleware())
    dp.include_router(router)

    log = logging.getLogger(__name__)
    if settings.is_debug:
        log.info(
            "Bot starting in DEBUG (only telegram_id=%s)…",
            settings.debug_telegram_id,
        )
    else:
        log.info("Bot starting in PROD (polling)…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
