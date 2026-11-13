"""Two-stage highlights: VL captions + text LLM selection."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from runware import Runware, file_to_base64

from bot.config import settings
from bot.video import SampledFrame

logger = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)

CAPTION_PROMPT = (
    "Describe this video frame in 1-2 short factual English sentences. "
    "Mention visible subjects, actions, on-screen text, and emotions. "
    "No opinions, no scores."
)


@dataclass(frozen=True)
class DescribedFrame:
    timestamp_sec: float
    caption: str
    path: Path


@dataclass(frozen=True)
class ScoredFrame:
    """Kept for fallback window selection."""

    timestamp_sec: float
    score: float
    reason: str
    path: Path


@dataclass(frozen=True)
class ClipWindow:
    start_sec: float
    end_sec: float
    score: float
    reason: str
    peak_sec: float


def build_rank_prompt(
    *,
    interest_criteria: str,
    duration_sec: float,
    frames: list[DescribedFrame],
    top_n: int,
    clip_min_sec: float,
    clip_max_sec: float,
    range_start_sec: float,
    range_end_sec: float,
) -> str:
    criteria = (interest_criteria or settings.default_interest_prompt).strip()
    if len(criteria) > settings.max_interest_prompt_chars:
        criteria = criteria[: settings.max_interest_prompt_chars]

    lines = [f"[{f.timestamp_sec:.1f}s] {f.caption}" for f in frames]
    timeline = "\n".join(lines)
    return (
        "You are a video highlight editor.\n"
        "Given a timeline of frame descriptions and the user's criteria, "
        f"pick up to {top_n} non-overlapping highlight clips.\n\n"
        f"Video duration: {duration_sec:.1f}s\n"
        f"Allowed analysis range: {range_start_sec:.1f}s – {range_end_sec:.1f}s "
        "(clips MUST stay inside this range)\n"
        f"Each clip length: between {clip_min_sec:.0f} and {clip_max_sec:.0f} seconds\n"
        f"User criteria: {criteria}\n\n"
        "Timeline:\n"
        f"{timeline}\n\n"
        "Rules:\n"
        "- Follow the user criteria carefully (topic, mood, people, text, etc.).\n"
        "- Prefer peaks that clearly match the criteria over random action.\n"
        "- Do not invent events that are not supported by the timeline.\n"
        "- Clips must not overlap.\n"
        "- Return ONLY compact JSON, no markdown:\n"
        '{"clips":[{"start":12.0,"end":20.0,"score":8,"reason":"short Russian phrase","peak":15.0}]}'
    )


def parse_score_response(text: str) -> tuple[float, str]:
    """Legacy helper kept for tests."""
    if not text:
        return 0.0, ""
    raw = text.strip()
    candidates = [raw]
    match = _JSON_RE.search(raw)
    if match:
        candidates.insert(0, match.group(0))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        try:
            score = float(data.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(10.0, score))
        reason = str(data.get("reason") or "").strip()
        return score, reason

    num = re.search(r"\b([1-9]|10)(?:\.\d+)?\b", raw)
    if num:
        return float(num.group(1)), raw[:80]
    return 0.0, ""


def parse_llm_clips_response(
    text: str,
    *,
    duration_sec: float,
    top_n: int,
    clip_min_sec: float,
    clip_max_sec: float,
    range_start_sec: float = 0.0,
    range_end_sec: float | None = None,
) -> list[ClipWindow]:
    if not text:
        return []
    raw = text.strip()
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    payload: object | None = None
    for candidate in (raw,):
        try:
            payload = json.loads(candidate)
            break
        except json.JSONDecodeError:
            pass
    if payload is None:
        obj = _JSON_RE.search(raw)
        arr = _JSON_ARRAY_RE.search(raw)
        for blob in filter(None, [obj.group(0) if obj else None, arr.group(0) if arr else None]):
            try:
                payload = json.loads(blob)
                break
            except json.JSONDecodeError:
                continue
    if payload is None:
        return []

    items: list
    if isinstance(payload, dict):
        items = payload.get("clips") or payload.get("windows") or []
    elif isinstance(payload, list):
        items = payload
    else:
        return []

    range_end = duration_sec if range_end_sec is None else range_end_sec
    windows: list[ClipWindow] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start", item.get("start_sec", -1)))
            end = float(item.get("end", item.get("end_sec", -1)))
            score = float(item.get("score", 7))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        peak = item.get("peak", item.get("peak_sec"))
        try:
            peak_sec = float(peak) if peak is not None else (start + end) / 2
        except (TypeError, ValueError):
            peak_sec = (start + end) / 2
        reason = str(item.get("reason") or "интересный момент").strip()

        start = max(range_start_sec, min(start, range_end))
        end = max(start + 0.5, min(end, range_end))
        # Clamp length
        if end - start > clip_max_sec:
            mid = (start + end) / 2
            start = max(range_start_sec, mid - clip_max_sec / 2)
            end = min(range_end, start + clip_max_sec)
        if end - start < max(1.0, clip_min_sec * 0.5):
            continue
        if end - start < clip_min_sec:
            need = clip_min_sec - (end - start)
            start = max(range_start_sec, start - need / 2)
            end = min(range_end, start + clip_min_sec)

        window = ClipWindow(
            start_sec=round(start, 3),
            end_sec=round(end, 3),
            score=max(0.0, min(10.0, score)),
            reason=reason,
            peak_sec=round(min(max(peak_sec, start), end), 3),
        )
        if any(_overlaps(window, existing) for existing in windows):
            continue
        windows.append(window)
        if len(windows) >= top_n:
            break

    windows.sort(key=lambda w: w.start_sec)
    return windows


async def caption_frame(client: Runware, frame: SampledFrame) -> DescribedFrame:
    image_b64 = file_to_base64(frame.path)
    results = await client.run(
        {
            "taskType": "caption",
            "model": settings.runware_caption_model,
            "prompt": CAPTION_PROMPT,
            "inputs": {"image": image_b64},
            "deliveryMethod": "sync",
        }
    )
    text = ""
    if results:
        first = results[0]
        if isinstance(first, dict):
            text = str(first.get("text") or "")
        else:
            text = str(getattr(first, "text", "") or "")
    caption = " ".join(text.split()) or "unclear frame"
    return DescribedFrame(
        timestamp_sec=frame.timestamp_sec,
        caption=caption,
        path=frame.path,
    )


async def caption_frames(
    frames: list[SampledFrame],
    *,
    concurrency: int = 4,
) -> list[DescribedFrame]:
    if not frames:
        return []

    semaphore = asyncio.Semaphore(concurrency)
    async with Runware(api_key=settings.runware_api_key, transport="rest") as client:

        async def _one(frame: SampledFrame) -> DescribedFrame:
            async with semaphore:
                try:
                    return await caption_frame(client, frame)
                except Exception:
                    logger.exception(
                        "Caption failed at t=%.1fs", frame.timestamp_sec
                    )
                    return DescribedFrame(
                        timestamp_sec=frame.timestamp_sec,
                        caption="",
                        path=frame.path,
                    )

        return list(await asyncio.gather(*[_one(f) for f in frames]))


async def _chat_completion(prompt: str) -> str:
    """Call Runware OpenAI-compatible chat API for text LLM ranking."""
    url = f"{settings.runware_chat_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.runware_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.runware_llm_model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": "You select video highlights. Reply with JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=body) as resp:
            raw = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"LLM HTTP {resp.status}: {raw[:400]}")
            data = json.loads(raw)
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


async def select_windows_with_llm(
    frames: list[DescribedFrame],
    *,
    interest_criteria: str,
    duration_sec: float,
    top_n: int,
    clip_min_sec: float,
    clip_max_sec: float,
    range_start_sec: float = 0.0,
    range_end_sec: float | None = None,
) -> list[ClipWindow]:
    usable = [f for f in frames if f.caption.strip()]
    if not usable:
        return []

    range_end = duration_sec if range_end_sec is None else range_end_sec
    prompt = build_rank_prompt(
        interest_criteria=interest_criteria,
        duration_sec=duration_sec,
        frames=usable,
        top_n=top_n,
        clip_min_sec=clip_min_sec,
        clip_max_sec=clip_max_sec,
        range_start_sec=range_start_sec,
        range_end_sec=range_end,
    )
    logger.info(
        "LLM rank prompt (%d frames, model=%s):\n%s",
        len(usable),
        settings.runware_llm_model,
        prompt[:1500],
    )
    text = await _chat_completion(prompt)
    logger.info("LLM rank response: %s", (text or "")[:1000])
    return parse_llm_clips_response(
        text,
        duration_sec=duration_sec,
        top_n=top_n,
        clip_min_sec=clip_min_sec,
        clip_max_sec=clip_max_sec,
        range_start_sec=range_start_sec,
        range_end_sec=range_end,
    )


def _softmax_sample(items: list[ScoredFrame], *, temperature: float) -> ScoredFrame:
    temp = max(0.05, temperature)
    weights = [math.exp(s.score / temp) for s in items]
    total = sum(weights)
    if total <= 0:
        return random.choice(items)
    r = random.uniform(0, total)
    acc = 0.0
    for item, w in zip(items, weights):
        acc += w
        if r <= acc:
            return item
    return items[-1]


def _window_from_frame(
    frame: ScoredFrame,
    *,
    duration_sec: float,
    half: float,
    clip_min_sec: float,
) -> ClipWindow | None:
    start = max(0.0, frame.timestamp_sec - half)
    end = min(duration_sec, frame.timestamp_sec + half)
    if end - start < clip_min_sec:
        need = clip_min_sec - (end - start)
        start = max(0.0, start - need / 2)
        end = min(duration_sec, start + clip_min_sec)
        start = max(0.0, end - clip_min_sec)
    if end - start < 1.0:
        return None
    return ClipWindow(
        start_sec=round(start, 3),
        end_sec=round(end, 3),
        score=frame.score,
        reason=frame.reason,
        peak_sec=frame.timestamp_sec,
    )


def _overlaps(a: ClipWindow, b: ClipWindow) -> bool:
    return a.start_sec < b.end_sec and a.end_sec > b.start_sec


def select_top_windows(
    scored: list[ScoredFrame],
    *,
    duration_sec: float,
    top_n: int = 3,
    clip_min_sec: float = 5.0,
    clip_max_sec: float = 12.0,
    min_score: float = 5.0,
    frame_interval_sec: float = 2.0,
    temperature: float = 1.2,
    pool_size: int = 8,
) -> list[ClipWindow]:
    """Fallback selector used if LLM ranking fails."""
    if duration_sec <= 0 or not scored:
        return []

    half = min(clip_max_sec, max(clip_min_sec, frame_interval_sec * 3)) / 2.0
    ranked = sorted(
        [s for s in scored if s.score >= min_score],
        key=lambda s: (-s.score, s.timestamp_sec),
    )
    if not ranked:
        ranked = sorted(
            [s for s in scored if s.score > 0],
            key=lambda s: (-s.score, s.timestamp_sec),
        )

    pool = ranked[: max(top_n, pool_size)]
    windows: list[ClipWindow] = []
    remaining = list(pool)

    while remaining and len(windows) < top_n:
        pick = _softmax_sample(remaining, temperature=temperature)
        remaining = [s for s in remaining if s is not pick]
        window = _window_from_frame(
            pick,
            duration_sec=duration_sec,
            half=half,
            clip_min_sec=clip_min_sec,
        )
        if window is None:
            continue
        if any(_overlaps(window, existing) for existing in windows):
            continue
        windows.append(window)

    windows.sort(key=lambda w: w.start_sec)
    return windows


def fallback_windows_from_captions(
    frames: list[DescribedFrame],
    *,
    interest_criteria: str,
    duration_sec: float,
    top_n: int,
    clip_min_sec: float,
    clip_max_sec: float,
    frame_interval_sec: float,
    temperature: float,
    pool_size: int,
) -> list[ClipWindow]:
    """Keyword-overlap fallback if LLM is unavailable."""
    tokens = {
        t
        for t in re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]{3,}", interest_criteria.lower())
        if t
    }
    scored: list[ScoredFrame] = []
    for frame in frames:
        if not frame.caption:
            continue
        caption_l = frame.caption.lower()
        hits = sum(1 for t in tokens if t in caption_l)
        score = 5.0 + min(5.0, hits * 1.5) if tokens else 6.0
        scored.append(
            ScoredFrame(
                timestamp_sec=frame.timestamp_sec,
                score=score,
                reason=frame.caption[:80],
                path=frame.path,
            )
        )
    return select_top_windows(
        scored,
        duration_sec=duration_sec,
        top_n=top_n,
        clip_min_sec=clip_min_sec,
        clip_max_sec=clip_max_sec,
        min_score=5.0,
        frame_interval_sec=frame_interval_sec,
        temperature=temperature,
        pool_size=pool_size,
    )
