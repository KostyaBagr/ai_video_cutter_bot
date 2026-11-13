"""Local ffmpeg helpers: duration, frame sampling, clip cutting."""

from __future__ import annotations

import asyncio
import json
import random
import shutil
from dataclasses import dataclass
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise FFmpegError(
            "ffmpeg не найден в PATH. Установите ffmpeg и перезапустите бота."
        )
    return path


def require_ffprobe() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise FFmpegError(
            "ffprobe не найден в PATH. Установите ffmpeg (включает ffprobe)."
        )
    return path


async def run_ffmpeg(cmd: list[str]) -> tuple[int, str, str]:
    """Run ffmpeg/ffprobe argv list (no shell). Returns (code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


# Back-compat alias for older call sites / tests
_run = run_ffmpeg


async def get_duration_sec(video_path: Path) -> float:
    ffprobe = require_ffprobe()
    code, out, err = await _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(video_path),
        ]
    )
    if code != 0:
        raise FFmpegError(f"Не удалось прочитать длительность: {err.strip()}")
    data = json.loads(out or "{}")
    duration = float(data.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise FFmpegError("Длительность видео равна нулю или неизвестна.")
    return duration


@dataclass(frozen=True)
class SampledFrame:
    path: Path
    timestamp_sec: float


async def sample_frames(
    video_path: Path,
    out_dir: Path,
    *,
    interval_sec: float = 2.0,
    max_frames: int = 30,
    start_offset_sec: float | None = None,
    range_start_sec: float = 0.0,
    range_end_sec: float | None = None,
) -> list[SampledFrame]:
    """Extract JPEG frames every `interval_sec`, capped at `max_frames`.

    Optional `range_start_sec`/`range_end_sec` limit sampling to a segment
    (e.g. last 30 seconds). `start_offset_sec` adds a random phase inside
    the range when None.
    """
    require_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = await get_duration_sec(video_path)

    range_start = max(0.0, min(range_start_sec, duration))
    range_end = duration if range_end_sec is None else max(range_start, min(range_end_sec, duration))
    span = max(0.0, range_end - range_start)

    # Spread frames across the whole analysis range (important for long videos)
    step = interval_sec
    if span > 0 and max_frames > 1:
        step = max(interval_sec, span / max_frames)

    if start_offset_sec is None:
        phase = random.uniform(0.0, max(0.01, min(step, max(span, 0.01))))
        start_offset_sec = range_start + phase
    start_offset_sec = max(range_start, min(start_offset_sec, max(range_start, range_end - 0.05)))

    timestamps: list[float] = []
    t = start_offset_sec
    while t < range_end and len(timestamps) < max_frames:
        timestamps.append(round(t, 3))
        t += step

    if not timestamps:
        timestamps = [round(range_start, 3)]

    frames: list[SampledFrame] = []
    ffmpeg = require_ffmpeg()
    for i, ts in enumerate(timestamps):
        out_path = out_dir / f"frame_{i:04d}.jpg"
        code, _, err = await _run(
            [
                ffmpeg,
                "-y",
                "-ss",
                f"{ts:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(out_path),
            ]
        )
        if code != 0 or not out_path.exists():
            raise FFmpegError(f"Не удалось извлечь кадр на {ts:.1f}s: {err.strip()}")
        frames.append(SampledFrame(path=out_path, timestamp_sec=ts))
    return frames


async def extract_mono_wav(
    video_path: Path,
    wav_path: Path,
    *,
    sample_rate: int = 16000,
) -> Path:
    """Extract mono PCM WAV for speech recognition."""
    ffmpeg = require_ffmpeg()
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    code, _, err = await _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ]
    )
    if code != 0 or not wav_path.exists():
        raise FFmpegError(f"Не удалось извлечь аудио: {err.strip()}")
    return wav_path


async def cut_clip(
    video_path: Path,
    out_path: Path,
    *,
    start_sec: float,
    end_sec: float,
    video_filter: str | None = None,
) -> Path:
    """Cut [start_sec, end_sec) with re-encode for accurate boundaries.

    Optional `video_filter` (ffmpeg -vf), e.g. TikTok 9:16 crop.
    """
    ffmpeg = require_ffmpeg()
    if end_sec <= start_sec:
        raise FFmpegError("Конец клипа должен быть позже начала.")

    duration = max(0.1, end_sec - start_sec)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        f"{start_sec:.3f}",
        "-i",
        str(video_path),
        "-t",
        f"{duration:.3f}",
    ]
    if video_filter:
        cmd.extend(["-vf", video_filter])
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
    )
    code, _, err = await _run(cmd)
    if code != 0 or not out_path.exists():
        raise FFmpegError(f"Не удалось нарезать клип: {err.strip()}")
    return out_path
