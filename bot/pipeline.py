"""Download → caption → LLM rank → cut orchestration."""

from __future__ import annotations

import logging
import math
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from bot.config import settings
from bot.formats import TIKTOK_VF, FORMAT_LABELS, OutputFormat
from bot.runware_client import (
    ClipWindow,
    caption_frames,
    fallback_windows_from_captions,
    select_windows_with_llm,
)
from bot.subtitles import burn_subtitles
from bot.time_window import parse_analysis_window
from bot.video import cut_clip, get_duration_sec, sample_frames

logger = logging.getLogger(__name__)

OnClip = Callable[["HighlightClip"], Awaitable[None]]
OnProgress = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class HighlightClip:
    path: Path
    window: ClipWindow
    index: int


@dataclass
class PipelineResult:
    clips: list[HighlightClip]
    work_dir: Path
    duration_sec: float
    output_format: OutputFormat

    def cleanup(self) -> None:
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir, ignore_errors=True)


class PipelineError(RuntimeError):
    pass


def _vf_for_format(output_format: OutputFormat) -> str | None:
    if output_format == OutputFormat.TIKTOK:
        return TIKTOK_VF
    return None


def _clip_bounds(clip_duration_sec: float | None) -> tuple[float, float]:
    """Return (min, max) clip length for LLM / fallback selection."""
    if clip_duration_sec is None:
        return settings.clip_min_sec, settings.clip_max_sec
    target = float(clip_duration_sec)
    # Small slack so LLM can place cuts on natural boundaries
    lo = max(settings.clip_duration_min_sec, target - 1.0)
    hi = min(settings.clip_duration_max_sec, target + 1.0)
    if hi < lo:
        hi = lo
    return lo, hi


def sequential_windows(
    duration_sec: float,
    piece_sec: float,
    *,
    max_clips: int | None = None,
) -> list[ClipWindow]:
    """Split [0, duration) into consecutive non-overlapping pieces."""
    if duration_sec <= 0 or piece_sec <= 0:
        return []
    limit = max_clips if max_clips is not None else settings.sequential_max_clips
    windows: list[ClipWindow] = []
    start = 0.0
    while start < duration_sec - 0.05 and len(windows) < limit:
        end = min(duration_sec, start + piece_sec)
        if end - start < 1.0:
            break
        mid = (start + end) / 2.0
        windows.append(
            ClipWindow(
                start_sec=round(start, 3),
                end_sec=round(end, 3),
                score=10.0,
                reason="sequential",
                peak_sec=round(mid, 3),
            )
        )
        start = end
    return windows


def _iter_chunks(
    range_start: float,
    range_end: float,
    chunk_sec: float,
) -> list[tuple[float, float]]:
    span = max(0.0, range_end - range_start)
    if span <= 0:
        return [(range_start, range_end)]
    if span <= chunk_sec * 1.25:
        return [(range_start, range_end)]
    n = max(1, math.ceil(span / chunk_sec))
    step = span / n
    chunks: list[tuple[float, float]] = []
    for i in range(n):
        start = range_start + i * step
        end = range_end if i == n - 1 else range_start + (i + 1) * step
        if end - start >= 1.0:
            chunks.append((start, end))
    return chunks or [(range_start, range_end)]


async def _emit_progress(on_progress: OnProgress | None, text: str) -> None:
    if on_progress is None:
        return
    try:
        await on_progress(text)
    except Exception:
        logger.exception("Progress callback failed")


async def _find_windows_in_range(
    video_path: Path,
    frames_dir: Path,
    *,
    criteria: str,
    duration_sec: float,
    range_start: float,
    range_end: float,
    top_n: int,
    clip_min: float,
    clip_max: float,
    max_frames: int,
) -> list[ClipWindow]:
    frames = await sample_frames(
        video_path,
        frames_dir,
        interval_sec=settings.frame_interval_sec,
        max_frames=max_frames,
        range_start_sec=range_start,
        range_end_sec=range_end,
    )
    logger.info(
        "Sampled %d frames for range %.1f–%.1f (budget=%d)",
        len(frames),
        range_start,
        range_end,
        max_frames,
    )

    described = await caption_frames(frames)
    logger.info(
        "Captioned %d/%d frames in range %.1f–%.1f",
        sum(1 for d in described if d.caption),
        len(described),
        range_start,
        range_end,
    )

    windows: list[ClipWindow] = []
    try:
        windows = await select_windows_with_llm(
            described,
            interest_criteria=criteria,
            duration_sec=duration_sec,
            top_n=top_n,
            clip_min_sec=clip_min,
            clip_max_sec=clip_max,
            range_start_sec=range_start,
            range_end_sec=range_end,
        )
    except Exception:
        logger.exception("LLM ranking failed; using caption keyword fallback")

    if not windows:
        windows = fallback_windows_from_captions(
            described,
            interest_criteria=criteria,
            duration_sec=duration_sec,
            top_n=top_n,
            clip_min_sec=clip_min,
            clip_max_sec=clip_max,
            frame_interval_sec=settings.frame_interval_sec,
            temperature=settings.selection_temperature,
            pool_size=settings.selection_pool_size,
        )
        clipped: list[ClipWindow] = []
        for w in windows:
            start = max(w.start_sec, range_start)
            end = min(w.end_sec, range_end)
            if end - start < 1.0:
                continue
            clipped.append(
                ClipWindow(
                    start_sec=round(start, 3),
                    end_sec=round(end, 3),
                    score=w.score,
                    reason=w.reason,
                    peak_sec=min(max(w.peak_sec, start), end),
                )
            )
        windows = clipped

    return windows[:top_n]


async def _cut_windows(
    video_path: Path,
    clips_dir: Path,
    windows: list[ClipWindow],
    *,
    output_format: OutputFormat,
    burn_subs: bool,
    on_clip: OnClip | None,
    on_progress: OnProgress | None,
) -> list[HighlightClip]:
    vf = _vf_for_format(output_format)
    clips: list[HighlightClip] = []
    total = len(windows)
    for window in windows:
        index = len(clips) + 1
        out = clips_dir / f"clip_{index}.mp4"
        await _emit_progress(
            on_progress,
            f"Готовлю клип {index} из {total}…",
        )
        await cut_clip(
            video_path,
            out,
            start_sec=window.start_sec,
            end_sec=window.end_sec,
            video_filter=vf,
        )
        final_path = out
        if burn_subs:
            await _emit_progress(
                on_progress,
                f"Добавляю субтитры к клипу {index}…",
            )
            subtitled = await burn_subtitles(out)
            if subtitled is not None:
                final_path = subtitled
        clip = HighlightClip(path=final_path, window=window, index=index)
        clips.append(clip)
        logger.info(
            "Cut clip %d (%.1f–%.1f) as %s — sending now",
            index,
            window.start_sec,
            window.end_sec,
            FORMAT_LABELS[output_format],
        )
        if on_clip is not None:
            try:
                await on_clip(clip)
            except Exception:
                logger.exception("Failed to deliver clip %d", index)
    return clips


async def process_sequential_video(
    video_path: Path,
    *,
    output_format: OutputFormat = OutputFormat.ORIGINAL,
    clip_duration_sec: float | None = None,
    burn_subs: bool = False,
    on_clip: OnClip | None = None,
    on_progress: OnProgress | None = None,
) -> PipelineResult:
    """Cut the whole video into consecutive equal-length pieces."""
    work_dir = Path(tempfile.mkdtemp(prefix="sequential_"))
    clips_dir = work_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    piece = (
        float(clip_duration_sec)
        if clip_duration_sec is not None
        else float(settings.sequential_default_clip_sec)
    )
    piece = max(
        settings.clip_duration_min_sec,
        min(settings.clip_duration_max_sec, piece),
    )

    try:
        duration = await get_duration_sec(video_path)
        if duration > settings.max_duration_sec:
            minutes = int(settings.max_duration_sec // 60)
            raise PipelineError(
                f"Это видео слишком длинное.\nМожно до {minutes} минут."
            )

        needed = max(1, math.ceil(duration / piece))
        if needed > settings.sequential_max_clips:
            raise PipelineError(
                f"При длине {piece:.0f} сек получится слишком много кусков "
                f"({needed}).\nВыбери клип подлиннее "
                f"(макс. {settings.sequential_max_clips} штук)."
            )

        await _emit_progress(on_progress, "Режу видео на куски подряд…")
        windows = sequential_windows(
            duration,
            piece,
            max_clips=settings.sequential_max_clips,
        )
        if not windows:
            raise PipelineError("Не получилось нарезать видео. Попробуй другую длину.")

        clips = await _cut_windows(
            video_path,
            clips_dir,
            windows,
            output_format=output_format,
            burn_subs=burn_subs,
            on_clip=on_clip,
            on_progress=on_progress,
        )
        logger.info(
            "Sequential done: %d clips (%.1fs each) as %s",
            len(clips),
            piece,
            FORMAT_LABELS[output_format],
        )
        return PipelineResult(
            clips=clips,
            work_dir=work_dir,
            duration_sec=duration,
            output_format=output_format,
        )
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


async def process_video(
    video_path: Path,
    *,
    output_format: OutputFormat = OutputFormat.ORIGINAL,
    interest_criteria: str | None = None,
    clip_duration_sec: float | None = None,
    burn_subs: bool = False,
    on_clip: OnClip | None = None,
    on_progress: OnProgress | None = None,
) -> PipelineResult:
    work_dir = Path(tempfile.mkdtemp(prefix="highlights_"))
    frames_root = work_dir / "frames"
    clips_dir = work_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    criteria = interest_criteria or settings.default_interest_prompt
    clip_min, clip_max = _clip_bounds(clip_duration_sec)
    logger.info(
        "User interest prompt: %s | clip_duration=%s (bounds %.1f–%.1f) | "
        "burn_subs=%s",
        criteria,
        clip_duration_sec,
        clip_min,
        clip_max,
        burn_subs,
    )

    try:
        duration = await get_duration_sec(video_path)
        if duration > settings.max_duration_sec:
            minutes = int(settings.max_duration_sec // 60)
            raise PipelineError(
                f"Это видео слишком длинное.\nМожно до {minutes} минут."
            )

        analysis = parse_analysis_window(criteria, duration_sec=duration)
        range_start = analysis.start_sec if analysis else 0.0
        range_end = analysis.end_sec if analysis else duration
        if analysis:
            logger.info("Applied time window from prompt: %s", analysis.label)

        chunks = _iter_chunks(
            range_start,
            range_end,
            settings.analysis_chunk_sec,
        )
        n_chunks = len(chunks)
        frame_budget = max(
            10,
            math.ceil(settings.max_frames / max(1, n_chunks)),
        )

        logger.info(
            "Processing %.1fs video as %d chunk(s), format=%s, frames/chunk≈%d",
            duration,
            n_chunks,
            output_format,
            frame_budget,
        )

        selected: list[ClipWindow] = []
        remaining = settings.top_clips

        for chunk_i, (chunk_start, chunk_end) in enumerate(chunks, start=1):
            if remaining <= 0:
                break

            # Spread remaining quota across leftover chunks
            left_chunks = n_chunks - chunk_i + 1
            want = max(1, math.ceil(remaining / left_chunks))
            want = min(want, remaining)

            await _emit_progress(
                on_progress,
                f"Смотрю часть {chunk_i} из {n_chunks}…",
            )

            chunk_frames_dir = frames_root / f"chunk_{chunk_i:02d}"
            windows = await _find_windows_in_range(
                video_path,
                chunk_frames_dir,
                criteria=criteria,
                duration_sec=duration,
                range_start=chunk_start,
                range_end=chunk_end,
                top_n=want,
                clip_min=clip_min,
                clip_max=clip_max,
                max_frames=frame_budget,
            )

            if not windows:
                logger.info(
                    "No highlights in chunk %d (%.1f–%.1f)",
                    chunk_i,
                    chunk_start,
                    chunk_end,
                )
                continue

            for window in windows:
                if remaining <= 0:
                    break
                selected.append(window)
                remaining -= 1

        clips = await _cut_windows(
            video_path,
            clips_dir,
            selected,
            output_format=output_format,
            burn_subs=burn_subs,
            on_clip=on_clip,
            on_progress=on_progress,
        )

        if not clips:
            raise PipelineError(
                "Не нашёл ярких моментов.\n"
                "Попробуй другое видео или опиши, что искать, иначе."
            )

        logger.info(
            "Done: %d clips as %s",
            len(clips),
            FORMAT_LABELS[output_format],
        )
        return PipelineResult(
            clips=clips,
            work_dir=work_dir,
            duration_sec=duration,
            output_format=output_format,
        )
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
