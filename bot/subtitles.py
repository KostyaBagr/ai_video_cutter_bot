"""Local Whisper transcription + TikTok-style ASS burn-in."""

from __future__ import annotations

import asyncio
import logging
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from bot.config import settings
from bot.video import FFmpegError, extract_mono_wav, require_ffmpeg, run_ffmpeg

logger = logging.getLogger(__name__)

_model_lock = threading.Lock()
_whisper_model = None


@dataclass(frozen=True)
class TimedWord:
    start_sec: float
    end_sec: float
    text: str


@dataclass(frozen=True)
class SubtitleCue:
    start_sec: float
    end_sec: float
    text: str


def chunk_words(
    words: list[TimedWord],
    *,
    max_words: int | None = None,
) -> list[SubtitleCue]:
    """Group timed words into short on-screen cues."""
    limit = max_words if max_words is not None else settings.subtitle_max_words
    limit = max(1, int(limit))
    cues: list[SubtitleCue] = []
    buf: list[TimedWord] = []
    for word in words:
        text = (word.text or "").strip()
        if not text:
            continue
        cleaned = TimedWord(
            start_sec=max(0.0, float(word.start_sec)),
            end_sec=max(float(word.start_sec), float(word.end_sec)),
            text=text,
        )
        buf.append(cleaned)
        if len(buf) >= limit:
            cues.append(
                SubtitleCue(
                    start_sec=buf[0].start_sec,
                    end_sec=max(w.end_sec for w in buf),
                    text=" ".join(w.text for w in buf),
                )
            )
            buf = []
    if buf:
        cues.append(
            SubtitleCue(
                start_sec=buf[0].start_sec,
                end_sec=max(w.end_sec for w in buf),
                text=" ".join(w.text for w in buf),
            )
        )
    return cues


def _ass_timestamp(sec: float) -> str:
    total_cs = max(0, int(round(sec * 100)))
    hours, rem = divmod(total_cs, 360000)
    minutes, rem = divmod(rem, 6000)
    seconds, centis = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def _escape_ass_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", "\\N")
    )


def build_ass_document(cues: list[SubtitleCue]) -> str:
    """TikTok-style: large white text, black outline, bottom-center."""
    font = settings.subtitle_font_name
    size = settings.subtitle_font_size
    margin_v = settings.subtitle_margin_v
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},&H00FFFFFF,&H000000FF,"
        f"&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,0,2,40,40,{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    lines: list[str] = []
    for cue in cues:
        if cue.end_sec <= cue.start_sec:
            continue
        text = _escape_ass_text(cue.text.upper())
        lines.append(
            "Dialogue: 0,"
            f"{_ass_timestamp(cue.start_sec)},{_ass_timestamp(cue.end_sec)},"
            f"Default,,0,0,0,,{text}"
        )
    return header + "\n".join(lines) + ("\n" if lines else "")


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    with _model_lock:
        if _whisper_model is not None:
            return _whisper_model
        from faster_whisper import WhisperModel

        logger.info(
            "Loading Whisper model=%s device=%s compute=%s",
            settings.whisper_model,
            settings.whisper_device,
            settings.whisper_compute_type,
        )
        _whisper_model = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )
        return _whisper_model


def _load_wav_float32(audio_path: Path) -> np.ndarray:
    """Load mono PCM WAV as float32 in [-1, 1].

    Pass ndarray into faster-whisper to avoid PyAV decode_audio
    (av 19 broke metadata_errors kwarg used by faster-whisper).
    """
    with wave.open(str(audio_path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if sample_width != 2:
        raise ValueError(f"Expected 16-bit PCM WAV, got sample_width={sample_width}")
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio


def _transcribe_words_sync(audio_path: Path) -> list[TimedWord]:
    model = _get_whisper_model()
    audio = _load_wav_float32(audio_path)
    segments, _info = model.transcribe(
        audio,
        word_timestamps=True,
        vad_filter=True,
    )
    words: list[TimedWord] = []
    for segment in segments:
        for word in segment.words or []:
            text = (word.word or "").strip()
            if not text:
                continue
            words.append(
                TimedWord(
                    start_sec=float(word.start or 0.0),
                    end_sec=float(word.end or word.start or 0.0),
                    text=text,
                )
            )
    return words


async def burn_ass_onto_video(
    video_path: Path,
    ass_path: Path,
    out_path: Path,
) -> Path:
    ffmpeg = require_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Escape path for ffmpeg filter (especially colons / specials)
    ass_escaped = (
        str(ass_path.resolve())
        .replace("\\", "/")
        .replace(":", "\\:")
        .replace("'", "\\'")
    )
    vf = f"ass='{ass_escaped}'"
    code, _, err = await run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
    )
    if code != 0 or not out_path.exists():
        raise FFmpegError(f"Не удалось наложить субтитры: {err.strip()}")
    return out_path


async def burn_subtitles(clip_path: Path) -> Path | None:
    """Transcribe clip and burn TikTok-style captions.

    Returns path to subtitled clip, or None if nothing to burn / on soft failure.
    """
    work = clip_path.parent
    wav_path = work / f"{clip_path.stem}_audio.wav"
    ass_path = work / f"{clip_path.stem}.ass"
    out_path = work / f"{clip_path.stem}_subs.mp4"

    try:
        await extract_mono_wav(clip_path, wav_path)
        words = await asyncio.to_thread(_transcribe_words_sync, wav_path)
        if not words:
            logger.info("No speech detected in %s — skipping subtitles", clip_path.name)
            return None

        cues = chunk_words(words, max_words=settings.subtitle_max_words)
        if not cues:
            logger.info("Empty cues for %s — skipping subtitles", clip_path.name)
            return None

        ass_path.write_text(build_ass_document(cues), encoding="utf-8")
        await burn_ass_onto_video(clip_path, ass_path, out_path)
        logger.info(
            "Burned %d subtitle cues onto %s",
            len(cues),
            clip_path.name,
        )
        return out_path
    except Exception:
        logger.exception("Subtitle burn failed for %s", clip_path)
        return None
    finally:
        for tmp in (wav_path, ass_path):
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
