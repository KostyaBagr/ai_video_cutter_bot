"""Output format options for cut clips."""

from __future__ import annotations

from enum import StrEnum


class OutputFormat(StrEnum):
    ORIGINAL = "original"
    TIKTOK = "tiktok"


class CutMode(StrEnum):
    HIGHLIGHTS = "highlights"
    SEQUENTIAL = "sequential"


FORMAT_LABELS = {
    OutputFormat.ORIGINAL: "как в оригинале",
    OutputFormat.TIKTOK: "вертикальный",
}

CUT_MODE_LABELS = {
    CutMode.HIGHLIGHTS: "яркие моменты",
    CutMode.SEQUENTIAL: "нарезка подряд",
}

# Vertical 1080×1920 center-crop (TikTok / Reels / Shorts)
TIKTOK_VF = (
    "scale=1080:1920:force_original_aspect_ratio=increase,"
    "crop=1080:1920,setsar=1"
)
