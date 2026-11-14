"""Telegram handlers: /start, MP4 upload + prompt + duration + format + subs."""

from __future__ import annotations

import logging
import math
import re
import uuid
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from bot.billing import (
    DIAMOND_PACKAGES,
    DiamondPackage,
    estimate_job_cost,
    minimum_job_cost,
    package_by_id,
    parse_invoice_payload,
    tariffs_text,
)
from bot.config import settings
from bot.db import (
    add_diamonds,
    charge_diamonds,
    clear_busy,
    credit_purchase_once,
    get_balance,
    is_busy,
    try_set_busy,
    upsert_user,
)
from bot.formats import CUT_MODE_LABELS, FORMAT_LABELS, CutMode, OutputFormat
from bot.pipeline import (
    HighlightClip,
    PipelineError,
    process_sequential_video,
    process_video,
)
from bot.video import FFmpegError, get_duration_sec

logger = logging.getLogger(__name__)
router = Router()

_GENERIC_FAIL = "Что-то пошло не так. Попробуй ещё раз чуть позже."
_UPLOAD_FAIL = "Не получилось принять файл. Пришли MP4 ещё раз."
_NEED_MP4 = "Пришли видео файлом MP4, не ссылкой."
_GONE = "Это видео уже недоступно. Пришли MP4 ещё раз."
_BUSY = "Сейчас уже обрабатываю твоё видео. Дождись результата."
_PAYMENTS_NOT_CONFIGURED = (
    "Оплата пока не настроена.\n"
    "Администратору нужно указать YOOKASSA_PROVIDER_TOKEN в .env "
    "(токен из @BotFather → Payments → ЮKassa)."
)


async def _ensure_user(message: Message):
    user = message.from_user
    if user is None:
        return await upsert_user(message.chat.id)
    return await upsert_user(
        user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )


async def _release_user(telegram_id: int | None) -> None:
    if telegram_id is None:
        return
    try:
        await clear_busy(telegram_id)
    except Exception:
        logger.exception("Failed to clear busy for %s", telegram_id)


class HighlightStates(StatesGroup):
    choosing_cut_mode = State()
    entering_prompt = State()
    choosing_clip_duration = State()
    choosing_format = State()
    choosing_subtitles = State()


def _cut_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Яркие моменты",
                    callback_data=f"cut:{CutMode.HIGHLIGHTS}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Нарезать подряд",
                    callback_data=f"cut:{CutMode.SEQUENTIAL}",
                ),
            ],
        ]
    )


def _prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Любые яркие моменты",
                    callback_data="prompt:default",
                )
            ]
        ]
    )


def _auto_duration_label(cut_mode: CutMode | None) -> str:
    if cut_mode == CutMode.SEQUENTIAL:
        sec = int(settings.sequential_default_clip_sec)
        return f"Авто · {sec} сек"
    return "Авто · 5–12 сек"


def _duration_keyboard(cut_mode: CutMode | None = None) -> InlineKeyboardMarkup:
    choices = list(settings.clip_duration_choices)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for sec in choices:
        row.append(
            InlineKeyboardButton(
                text=f"{sec} сек",
                callback_data=f"dur:{sec}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                text=_auto_duration_label(cut_mode),
                callback_data="dur:default",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Как в оригинале",
                    callback_data=f"fmt:{OutputFormat.ORIGINAL}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Вертикальный · для TikTok",
                    callback_data=f"fmt:{OutputFormat.TIKTOK}",
                ),
            ],
        ]
    )


def _subtitles_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="С субтитрами",
                    callback_data="subs:yes",
                ),
                InlineKeyboardButton(
                    text="Без субтитров",
                    callback_data="subs:no",
                ),
            ]
        ]
    )


def _shop_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for pkg in DIAMOND_PACKAGES:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Купить {pkg.title}",
                    callback_data=f"shop:buy:{pkg.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _shop_entry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Купить алмазы",
                    callback_data="shop:open",
                )
            ]
        ]
    )


def _max_minutes() -> int:
    return int(settings.max_duration_sec // 60)


async def _send_shop(message: Message, *, balance: int) -> None:
    lines = [
        f"Баланс: {balance} 💎\n",
        "Пакеты алмазов (оплата через ЮKassa):",
    ]
    for pkg in DIAMOND_PACKAGES:
        lines.append(f"• {pkg.title} — {pkg.price_rub} ₽ ({pkg.subtitle})")
    await message.answer("\n".join(lines), reply_markup=_shop_keyboard())


async def _send_package_invoice(message: Message, pkg: DiamondPackage) -> None:
    token = (settings.yookassa_provider_token or "").strip()
    if not token:
        await message.answer(_PAYMENTS_NOT_CONFIGURED)
        return
    await message.answer_invoice(
        title=pkg.title,
        description=f"{pkg.diamonds} алмазов для монтажа в боте",
        payload=f"diamonds:{pkg.id}",
        provider_token=token,
        currency="RUB",
        prices=[
            LabeledPrice(
                label=f"{pkg.diamonds} алмазов",
                amount=pkg.price_kopecks,
            )
        ],
    )


async def _resolve_clip_count(
    video_path: Path,
    cut_mode: CutMode,
    clip_duration_sec: float | None,
) -> int:
    if cut_mode != CutMode.SEQUENTIAL:
        return int(settings.top_clips)
    piece = (
        float(clip_duration_sec)
        if clip_duration_sec is not None
        else float(settings.sequential_default_clip_sec)
    )
    piece = max(
        settings.clip_duration_min_sec,
        min(settings.clip_duration_max_sec, piece),
    )
    duration = await get_duration_sec(video_path)
    needed = max(1, math.ceil(duration / piece))
    return min(int(settings.sequential_max_clips), needed)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    db_user = await _ensure_user(message)
    # Recover from interrupted flow
    await clear_busy(db_user.telegram_id)
    await message.answer(
        "Пришли видео файлом MP4.\n\n"
        "Могу найти яркие моменты или нарезать ролик подряд на короткие клипы.\n"
        f"Подойдут ролики до {_max_minutes()} минут "
        f"и до {settings.max_file_bytes // (1024 * 1024)} МБ.\n\n"
        f"Баланс: {db_user.diamonds} 💎\n"
        "Команды: /balance · /prices · /shop",
        reply_markup=_shop_entry_keyboard(),
    )


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    db_user = await _ensure_user(message)
    await message.answer(
        f"Баланс: {db_user.diamonds} 💎",
        reply_markup=_shop_entry_keyboard(),
    )


@router.message(Command("prices"))
async def cmd_prices(message: Message) -> None:
    await _ensure_user(message)
    await message.answer(tariffs_text(), reply_markup=_shop_entry_keyboard())


@router.message(Command("shop"))
async def cmd_shop(message: Message) -> None:
    db_user = await _ensure_user(message)
    await _send_shop(message, balance=db_user.diamonds)


@router.callback_query(F.data == "shop:open")
async def on_shop_open(callback: CallbackQuery) -> None:
    await callback.answer()
    user = callback.from_user
    if user is None:
        return
    db_user = await upsert_user(
        user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )
    status = callback.message
    if status is None:
        return
    await _send_shop(status, balance=db_user.diamonds)


@router.callback_query(F.data.startswith("shop:buy:"))
async def on_shop_buy(callback: CallbackQuery) -> None:
    raw = (callback.data or "").removeprefix("shop:buy:")
    pkg = package_by_id(raw)
    if pkg is None:
        await callback.answer("Пакет не найден", show_alert=True)
        return
    await callback.answer()
    status = callback.message
    if status is None:
        return
    try:
        await _send_package_invoice(status, pkg)
    except Exception:
        logger.exception("Failed to send invoice for package %s", pkg.id)
        await status.answer(
            "Не удалось открыть оплату. Попробуй ещё раз чуть позже."
        )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    pkg_id = parse_invoice_payload(query.invoice_payload or "")
    if pkg_id is None:
        await query.answer(
            ok=False,
            error_message="Неизвестный пакет. Открой /shop и выбери снова.",
        )
        return
    pkg = package_by_id(pkg_id)
    assert pkg is not None
    if query.currency != "RUB" or int(query.total_amount) != pkg.price_kopecks:
        await query.answer(
            ok=False,
            error_message="Сумма счёта устарела. Открой /shop и выбери пакет снова.",
        )
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message) -> None:
    payment = message.successful_payment
    if payment is None:
        return
    user = message.from_user
    if user is None:
        return

    pkg_id = parse_invoice_payload(payment.invoice_payload or "")
    pkg = package_by_id(pkg_id) if pkg_id else None
    if pkg is None:
        logger.error(
            "Successful payment with unknown payload=%s user=%s",
            payment.invoice_payload,
            user.id,
        )
        await message.answer(
            "Оплата прошла, но пакет не распознан. Напиши в поддержку."
        )
        return

    await upsert_user(
        user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )
    charge_id = (
        (payment.provider_payment_charge_id or "").strip()
        or (payment.telegram_payment_charge_id or "").strip()
    )
    if not charge_id:
        # Never key idempotency by amount alone — collide across payments.
        charge_id = f"missing-{user.id}-{uuid.uuid4().hex}"
        logger.error(
            "Payment missing charge ids user=%s pkg=%s amount=%s; using %s",
            user.id,
            pkg.id,
            payment.total_amount,
            charge_id,
        )
    credited, balance = await credit_purchase_once(
        user.id,
        pkg.diamonds,
        provider_charge_id=charge_id,
        package_id=pkg.id,
    )
    logger.info(
        "Payment user=%s pkg=%s charge=%s credited=%s balance=%s",
        user.id,
        pkg.id,
        charge_id,
        credited,
        balance,
    )
    if credited:
        await message.answer(
            f"Оплата прошла: +{pkg.diamonds} 💎\n"
            f"Баланс: {balance} 💎"
        )
    else:
        await message.answer(
            f"Эта оплата уже учтена.\nБаланс: {balance} 💎"
        )


def _normalize_prompt(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return settings.default_interest_prompt
    max_len = settings.max_interest_prompt_chars
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


def _parse_clip_duration(text: str) -> float | None:
    """Parse a clip length from user text like '15', '15с', '15 sec'."""
    raw = (text or "").strip().lower().replace(",", ".")
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(?:с|сек|секунд[аыу]?|s|sec|seconds?)?",
        raw,
    )
    if not match:
        return None
    value = float(match.group(1))
    if value < settings.clip_duration_min_sec or value > settings.clip_duration_max_sec:
        return None
    return value


def _is_mp4_message(message: Message) -> bool:
    video = message.video
    if video is not None:
        mime = (video.mime_type or "video/mp4").lower()
        name = (video.file_name or "").lower()
        return mime == "video/mp4" or name.endswith(".mp4") or mime == ""
    document = message.document
    if document is None:
        return False
    mime = (document.mime_type or "").lower()
    name = (document.file_name or "").lower()
    return mime == "video/mp4" or name.endswith(".mp4")


def _upload_media(message: Message):
    if message.video is not None and _is_mp4_message(message):
        return message.video
    if message.document is not None and _is_mp4_message(message):
        return message.document
    return None


async def _store_mp4(message: Message) -> Path:
    media = _upload_media(message)
    if media is None:
        raise ValueError("not mp4")
    if media.file_size and media.file_size > settings.max_file_bytes:
        raise ValueError("too big")
    bot = message.bot
    if bot is None:
        raise ValueError("no bot")
    remote = await bot.get_file(media.file_id)
    if not remote.file_path:
        raise ValueError("no file path")
    dest_dir = Path(settings.video_cache_dir).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", media.file_unique_id)[:80]
    dest = dest_dir / f"upload_{safe}.mp4"
    if not (dest.is_file() and dest.stat().st_size > 0):
        await bot.download_file(remote.file_path, destination=dest)
    return dest


async def _offer_cut_mode(
    status: Message,
    state: FSMContext,
    *,
    video_path: Path,
    chat_id: int,
    telegram_id: int,
    from_cache: bool = False,
) -> None:
    await state.set_state(HighlightStates.choosing_cut_mode)
    await state.set_data(
        {
            "video_path": str(video_path),
            "chat_id": chat_id,
            "telegram_id": telegram_id,
        }
    )
    ready = "Видео готово." if from_cache else "Видео загружено."
    await status.edit_text(
        f"{ready}\n\n"
        "Как нарезать видео?",
        reply_markup=_cut_mode_keyboard(),
    )


async def _offer_prompt(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(HighlightStates.entering_prompt)
    await state.set_data(data)
    await message.answer(
        "Что искать в ролике? Напиши своими словами, например:\n"
        "«смешные реакции», «когда говорят цену», «эпичные моменты».\n\n"
        "Или нажми кнопку ниже.",
        reply_markup=_prompt_keyboard(),
    )


async def _offer_clip_duration(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(HighlightStates.choosing_clip_duration)
    await state.set_data(data)
    raw_mode = data.get("cut_mode")
    try:
        cut_mode = CutMode(raw_mode) if raw_mode else None
    except ValueError:
        cut_mode = None
    await message.answer(
        "Какой длины сделать каждый клип?\n"
        "Выбери кнопку или напиши число секунд "
        f"({int(settings.clip_duration_min_sec)}–"
        f"{int(settings.clip_duration_max_sec)}).",
        reply_markup=_duration_keyboard(cut_mode),
    )


async def _offer_format_choice(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(HighlightStates.choosing_format)
    await state.set_data(data)
    clip_dur = data.get("clip_duration_sec")
    raw_mode = data.get("cut_mode")
    try:
        cut_mode = CutMode(raw_mode) if raw_mode else None
    except ValueError:
        cut_mode = None
    if isinstance(clip_dur, (int, float)):
        dur_line = f"Длина клипа: {clip_dur:.0f} сек\n"
    else:
        dur_line = f"Длина клипа: {_auto_duration_label(cut_mode).lower()}\n"
    await message.answer(
        f"{dur_line}В каком виде прислать клипы?",
        reply_markup=_format_keyboard(),
    )


async def _offer_subtitles_choice(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(HighlightStates.choosing_subtitles)
    await state.set_data(data)
    await message.answer(
        "Добавить субтитры на видео?",
        reply_markup=_subtitles_keyboard(),
    )


async def _run_pipeline(
    *,
    telegram_id: int,
    status_msg: Message,
    video_path: Path,
    output_format: OutputFormat,
    cut_mode: CutMode,
    interest_criteria: str,
    clip_duration_sec: float | None,
    burn_subs: bool,
) -> None:
    label = FORMAT_LABELS[output_format]
    mode_label = CUT_MODE_LABELS[cut_mode]

    if not await try_set_busy(telegram_id):
        await status_msg.edit_text(_BUSY, reply_markup=None)
        return

    job_id = uuid.uuid4().hex[:12]
    charged = 0

    try:
        clip_count = await _resolve_clip_count(
            video_path, cut_mode, clip_duration_sec
        )
    except FFmpegError as exc:
        logger.exception("Failed to probe duration for billing: %s", exc)
        await _release_user(telegram_id)
        await status_msg.edit_text(_GENERIC_FAIL, reply_markup=None)
        return

    cost = estimate_job_cost(
        cut_mode, burn_subs=burn_subs, clip_count=clip_count
    )
    charge_reason = f"job:{cut_mode.value}:{job_id}"
    if not await charge_diamonds(telegram_id, cost, reason=charge_reason):
        await _release_user(telegram_id)
        left = await get_balance(telegram_id)
        try:
            await status_msg.edit_text(
                "Не хватает алмазов для этой обработки.\n"
                f"Нужно {cost} 💎, у тебя {left} 💎.",
                reply_markup=_shop_entry_keyboard(),
            )
        except Exception:
            logger.exception("Failed to show insufficient diamonds message")
        return
    charged = cost

    start_status = (
        "Режу видео подряд…"
        if cut_mode == CutMode.SEQUENTIAL
        else "Ищу интересные моменты…"
    )
    try:
        await status_msg.edit_text(
            f"{start_status}\nСписано {cost} 💎.",
            reply_markup=None,
        )
    except Exception:
        logger.exception("Failed to update status before pipeline")

    sent = 0

    async def on_progress(text: str) -> None:
        try:
            await status_msg.edit_text(text)
        except Exception:
            logger.exception("Failed to update progress status")

    async def on_clip(clip: HighlightClip) -> None:
        nonlocal sent
        await status_msg.answer_video(video=FSInputFile(clip.path))
        sent += 1
        try:
            await status_msg.edit_text(
                f"Клип {sent} готов — продолжаю…"
            )
        except Exception:
            logger.exception("Failed to update status after clip send")

    async def _refund_if_nothing_sent() -> None:
        nonlocal charged
        if charged <= 0 or sent > 0:
            return
        try:
            await add_diamonds(
                telegram_id,
                charged,
                reason=f"refund:{cut_mode.value}:{job_id}",
            )
            logger.info(
                "Refunded %s diamonds to %s for failed job %s",
                charged,
                telegram_id,
                job_id,
            )
            charged = 0
        except Exception:
            logger.exception(
                "Failed to refund %s diamonds to %s job=%s",
                charged,
                telegram_id,
                job_id,
            )

    result = None
    try:
        if cut_mode == CutMode.SEQUENTIAL:
            result = await process_sequential_video(
                video_path,
                output_format=output_format,
                clip_duration_sec=clip_duration_sec,
                burn_subs=burn_subs,
                on_clip=on_clip,
                on_progress=on_progress,
            )
        else:
            result = await process_video(
                video_path,
                output_format=output_format,
                interest_criteria=interest_criteria,
                clip_duration_sec=clip_duration_sec,
                burn_subs=burn_subs,
                on_clip=on_clip,
                on_progress=on_progress,
            )

        left = await get_balance(telegram_id)
        delivered = sent if sent else len(result.clips)
        await status_msg.edit_text(
            f"Готово — {delivered} клип(ов), "
            f"{mode_label}, формат: {label}.\n"
            f"Списано {cost} 💎 · баланс: {left} 💎."
        )
    except PipelineError as exc:
        logger.warning("Pipeline error: %s", exc)
        await _refund_if_nothing_sent()
        msg = str(exc)
        if sent:
            await status_msg.edit_text(
                f"Уже отправил {sent}. Дальше не получилось — попробуй другое видео."
            )
        else:
            left = await get_balance(telegram_id)
            await status_msg.edit_text(
                f"{msg or _GENERIC_FAIL}\nАлмазы возвращены · баланс: {left} 💎."
            )
    except FFmpegError as exc:
        logger.exception("FFmpeg error during processing: %s", exc)
        await _refund_if_nothing_sent()
        if sent:
            await status_msg.edit_text(
                f"Уже отправил {sent}. Остальное обработать не вышло."
            )
        else:
            left = await get_balance(telegram_id)
            await status_msg.edit_text(
                f"{_GENERIC_FAIL}\nАлмазы возвращены · баланс: {left} 💎."
            )
    except Exception:
        logger.exception("Failed to process video for format %s", output_format)
        await _refund_if_nothing_sent()
        if sent:
            await status_msg.edit_text(
                f"Уже отправил {sent}. Дальше что-то пошло не так."
            )
        else:
            left = await get_balance(telegram_id)
            await status_msg.edit_text(
                f"{_GENERIC_FAIL}\nАлмазы возвращены · баланс: {left} 💎."
            )
    finally:
        await _release_user(telegram_id)
        if result is not None:
            result.cleanup()


@router.message(StateFilter(default_state), F.video)
@router.message(StateFilter(default_state), F.document)
async def on_mp4_upload(message: Message, state: FSMContext) -> None:
    if not _is_mp4_message(message):
        await message.answer(_NEED_MP4)
        return

    media = _upload_media(message)
    if media is not None and media.file_size and media.file_size > settings.max_file_bytes:
        limit_mb = settings.max_file_bytes // (1024 * 1024)
        await message.answer(f"Файл больше {limit_mb} МБ. Пришли более короткий MP4.")
        return

    db_user = await _ensure_user(message)
    telegram_id = db_user.telegram_id
    if await is_busy(telegram_id) or not await try_set_busy(telegram_id):
        await message.answer(_BUSY)
        return

    await state.clear()
    status = await message.answer("Принимаю видео…")
    try:
        video_path = await _store_mp4(message)
        duration = await get_duration_sec(video_path)
        if duration > settings.max_duration_sec:
            video_path.unlink(missing_ok=True)
            await status.edit_text(
                f"Это видео слишком длинное.\nМожно до {_max_minutes()} минут."
            )
            return
        await _release_user(telegram_id)
        await _offer_cut_mode(
            status,
            state,
            video_path=video_path,
            chat_id=message.chat.id,
            telegram_id=telegram_id,
        )
    except FFmpegError:
        logger.exception("Failed to read uploaded video")
        await state.clear()
        await status.edit_text(_UPLOAD_FAIL)
    except Exception:
        logger.exception("Failed to store uploaded MP4")
        await state.clear()
        await status.edit_text(_UPLOAD_FAIL)
    finally:
        await _release_user(telegram_id)


@router.message(F.animation)
@router.message(F.video_note)
async def on_unsupported_media(message: Message) -> None:
    await message.answer(_NEED_MP4)


@router.message(StateFilter(default_state), F.text)
async def on_text_instead_of_file(message: Message) -> None:
    await message.answer(_NEED_MP4)


@router.callback_query(
    StateFilter(HighlightStates.choosing_cut_mode),
    F.data.startswith("cut:"),
)
async def on_cut_mode_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    raw = (callback.data or "").removeprefix("cut:")
    try:
        cut_mode = CutMode(raw)
    except ValueError:
        await callback.answer("Выбери вариант из списка", show_alert=True)
        return

    data = await state.get_data()
    video_path = Path(data.get("video_path") or "")
    if not video_path.exists():
        await callback.answer(_GONE, show_alert=True)
        telegram_id = data.get("telegram_id")
        await state.clear()
        await _release_user(telegram_id)
        return

    await callback.answer()
    status_msg = callback.message
    if status_msg is None:
        return

    await state.update_data(cut_mode=cut_mode.value)
    await status_msg.edit_text(
        f"Режим: {CUT_MODE_LABELS[cut_mode]}",
        reply_markup=None,
    )

    if cut_mode == CutMode.SEQUENTIAL:
        await _offer_clip_duration(status_msg, state)
    else:
        await _offer_prompt(status_msg, state)


@router.message(StateFilter(HighlightStates.entering_prompt), F.text)
async def on_interest_prompt_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    video_path = Path(data.get("video_path") or "")
    if not video_path.exists():
        telegram_id = data.get("telegram_id")
        await state.clear()
        await _release_user(telegram_id)
        await message.answer(_GONE)
        return

    prompt = _normalize_prompt(message.text or "")
    logger.info("User prompt chat_id=%s: %s", message.chat.id, prompt)
    await state.update_data(interest_criteria=prompt)
    await message.answer(f"Ок, ищу: {prompt}")
    await _offer_clip_duration(message, state)


@router.callback_query(
    StateFilter(HighlightStates.entering_prompt),
    F.data == "prompt:default",
)
async def on_interest_prompt_default(
    callback: CallbackQuery, state: FSMContext
) -> None:
    data = await state.get_data()
    video_path = Path(data.get("video_path") or "")
    if not video_path.exists():
        await callback.answer(_GONE, show_alert=True)
        telegram_id = data.get("telegram_id")
        await state.clear()
        await _release_user(telegram_id)
        return

    await callback.answer()
    prompt = settings.default_interest_prompt
    logger.info(
        "User prompt chat_id=%s: [default] %s",
        callback.from_user.id if callback.from_user else "?",
        prompt,
    )
    await state.update_data(interest_criteria=prompt)

    status_msg = callback.message
    if status_msg is None:
        return
    await status_msg.edit_text(
        "Ок, ищу любые яркие моменты.",
        reply_markup=None,
    )
    await _offer_clip_duration(status_msg, state)


@router.message(StateFilter(HighlightStates.choosing_clip_duration), F.text)
async def on_clip_duration_text(message: Message, state: FSMContext) -> None:
    value = _parse_clip_duration(message.text or "")
    if value is None:
        await message.answer(
            "Напиши число секунд "
            f"({int(settings.clip_duration_min_sec)}–"
            f"{int(settings.clip_duration_max_sec)}) "
            "или нажми кнопку."
        )
        return

    logger.info(
        "User clip_duration chat_id=%s: %.1fs",
        message.chat.id,
        value,
    )
    await state.update_data(clip_duration_sec=value)
    await message.answer(f"Длина клипа: {value:.0f} сек")
    await _offer_format_choice(message, state)


@router.callback_query(
    StateFilter(HighlightStates.choosing_clip_duration),
    F.data.startswith("dur:"),
)
async def on_clip_duration_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    raw = (callback.data or "").removeprefix("dur:")
    status_msg = callback.message
    if status_msg is None:
        await callback.answer()
        return

    if raw == "default":
        await state.update_data(clip_duration_sec=None)
        data = await state.get_data()
        raw_mode = data.get("cut_mode")
        try:
            cut_mode = CutMode(raw_mode) if raw_mode else None
        except ValueError:
            cut_mode = None
        logger.info(
            "User clip_duration chat_id=%s: default",
            callback.from_user.id if callback.from_user else "?",
        )
        await callback.answer()
        await status_msg.edit_text(
            f"Длина клипа: {_auto_duration_label(cut_mode).lower()}",
            reply_markup=None,
        )
        await _offer_format_choice(status_msg, state)
        return

    try:
        value = float(raw)
    except ValueError:
        await callback.answer("Выбери длину из списка", show_alert=True)
        return

    if value < settings.clip_duration_min_sec or value > settings.clip_duration_max_sec:
        await callback.answer("Эта длина недоступна", show_alert=True)
        return

    await state.update_data(clip_duration_sec=value)
    logger.info(
        "User clip_duration chat_id=%s: %.1fs",
        callback.from_user.id if callback.from_user else "?",
        value,
    )
    await callback.answer()
    await status_msg.edit_text(
        f"Длина клипа: {value:.0f} сек",
        reply_markup=None,
    )
    await _offer_format_choice(status_msg, state)


@router.callback_query(
    StateFilter(HighlightStates.choosing_format),
    F.data.startswith("fmt:"),
)
async def on_format_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    raw = (callback.data or "").removeprefix("fmt:")
    try:
        output_format = OutputFormat(raw)
    except ValueError:
        await callback.answer("Выбери формат из списка", show_alert=True)
        return

    data = await state.get_data()
    video_path = Path(data.get("video_path") or "")
    if not video_path.exists():
        await callback.answer(_GONE, show_alert=True)
        telegram_id = data.get("telegram_id")
        await state.clear()
        await _release_user(telegram_id)
        return

    await callback.answer()
    label = FORMAT_LABELS[output_format]
    status_msg = callback.message
    if status_msg is None:
        return

    await state.update_data(output_format=output_format.value)
    await status_msg.edit_text(f"Формат: {label}", reply_markup=None)
    await _offer_subtitles_choice(status_msg, state)


@router.callback_query(
    StateFilter(HighlightStates.choosing_subtitles),
    F.data.in_({"subs:yes", "subs:no"}),
)
async def on_subtitles_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    burn_subs = (callback.data or "") == "subs:yes"
    data = await state.get_data()
    video_path = Path(data.get("video_path") or "")
    interest_criteria = data.get("interest_criteria") or settings.default_interest_prompt
    clip_duration_sec = data.get("clip_duration_sec")
    raw_format = data.get("output_format") or OutputFormat.ORIGINAL
    raw_mode = data.get("cut_mode") or CutMode.HIGHLIGHTS

    try:
        output_format = OutputFormat(raw_format)
        cut_mode = CutMode(raw_mode)
    except ValueError:
        await callback.answer("Выбери вариант из списка", show_alert=True)
        await state.clear()
        return

    telegram_id = data.get("telegram_id")
    if telegram_id is None and callback.from_user is not None:
        telegram_id = callback.from_user.id

    if not video_path.exists():
        await callback.answer(_GONE, show_alert=True)
        await state.clear()
        await _release_user(telegram_id)
        return

    if telegram_id is None:
        await callback.answer(_GENERIC_FAIL, show_alert=True)
        await state.clear()
        return

    # Fast pre-check before heavy work (exact charge happens in pipeline)
    min_cost = minimum_job_cost(cut_mode)
    balance = await get_balance(int(telegram_id))
    if balance < min_cost:
        await callback.answer()
        status_msg = callback.message
        await state.clear()
        await _release_user(telegram_id)
        if status_msg is not None:
            await status_msg.edit_text(
                "Не хватает алмазов для этой обработки.\n"
                f"Нужно минимум {min_cost} 💎, у тебя {balance} 💎.",
                reply_markup=_shop_entry_keyboard(),
            )
        return

    await callback.answer()
    status_msg = callback.message
    await state.clear()
    if status_msg is None:
        await _release_user(telegram_id)
        return

    logger.info(
        "User subtitles chat_id=%s: %s mode=%s",
        telegram_id,
        burn_subs,
        cut_mode,
    )
    await status_msg.edit_text(
        "С субтитрами." if burn_subs else "Без субтитров.",
        reply_markup=None,
    )
    await _run_pipeline(
        telegram_id=int(telegram_id),
        status_msg=status_msg,
        video_path=video_path,
        output_format=output_format,
        cut_mode=cut_mode,
        interest_criteria=interest_criteria,
        clip_duration_sec=clip_duration_sec,
        burn_subs=burn_subs,
    )


@router.callback_query(F.data.startswith("fmt:"))
@router.callback_query(F.data.startswith("dur:"))
@router.callback_query(F.data.startswith("subs:"))
@router.callback_query(F.data.startswith("cut:"))
@router.callback_query(F.data == "prompt:default")
async def on_stale_callback(callback: CallbackQuery) -> None:
    await callback.answer(
        "Этот шаг уже прошёл — пришли MP4 ещё раз.",
        show_alert=True,
    )
