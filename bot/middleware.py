"""Update filters / access control."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject, Update

from bot.config import settings

logger = logging.getLogger(__name__)


def _extract_user_id(event: TelegramObject) -> int | None:
    if isinstance(event, Message) and event.from_user is not None:
        return event.from_user.id
    if isinstance(event, CallbackQuery) and event.from_user is not None:
        return event.from_user.id
    if isinstance(event, PreCheckoutQuery) and event.from_user is not None:
        return event.from_user.id
    if isinstance(event, Update):
        if event.message and event.message.from_user:
            return event.message.from_user.id
        if event.callback_query and event.callback_query.from_user:
            return event.callback_query.from_user.id
        if event.pre_checkout_query and event.pre_checkout_query.from_user:
            return event.pre_checkout_query.from_user.id
    user = getattr(event, "from_user", None)
    if user is not None and getattr(user, "id", None) is not None:
        return int(user.id)
    return None


async def _deny_update(event: TelegramObject) -> None:
    """Reply when denying access (Update middleware sees Update, not Message)."""
    message: Message | None = None
    callback: CallbackQuery | None = None
    pre_checkout: PreCheckoutQuery | None = None

    if isinstance(event, Update):
        message = event.message
        callback = event.callback_query
        pre_checkout = event.pre_checkout_query
    elif isinstance(event, Message):
        message = event
    elif isinstance(event, CallbackQuery):
        callback = event
    elif isinstance(event, PreCheckoutQuery):
        pre_checkout = event

    if pre_checkout is not None:
        await pre_checkout.answer(
            ok=False,
            error_message="Бот в режиме разработки. Оплата недоступна.",
        )
        return
    if callback is not None:
        await callback.answer("Бот в режиме разработки.", show_alert=True)
        return
    if message is not None:
        await message.answer("Бот в режиме разработки. Доступ ограничен.")


class DebugAccessMiddleware(BaseMiddleware):
    """In DEBUG env, only one Telegram user id may talk to the bot."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not settings.is_debug:
            return await handler(event, data)

        allowed = int(settings.debug_telegram_id or 0)
        user_id = _extract_user_id(event)
        if allowed <= 0:
            logger.error("DEBUG mode without DEBUG_TELEGRAM_ID — dropping update")
            return None
        if user_id is None or user_id != allowed:
            logger.info(
                "DEBUG: ignore update from user_id=%s (allowed=%s)",
                user_id,
                allowed,
            )
            try:
                await _deny_update(event)
            except Exception:
                logger.exception("Failed to notify denied user_id=%s", user_id)
            return None
        return await handler(event, data)
