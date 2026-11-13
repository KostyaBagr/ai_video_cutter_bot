"""Parse temporal constraints from user interest prompts."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisWindow:
    start_sec: float
    end_sec: float
    label: str


_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT = r"(?:с(?:ек(?:унд[аыу]?)?)?|sec(?:onds?)?|s|м(?:ин(?:ут[аыу]?)?)?|min(?:utes?)?|m)"

# последние / last N sec|min
_LAST_RE = re.compile(
    rf"(?i)(?:последн\w*|конце?|в\s+конце|last|ending|final)\D{{0,20}}{_NUM}\s*{_UNIT}"
)
# первые / first N
_FIRST_RE = re.compile(
    rf"(?i)(?:перв\w*|начале?|в\s+начале|first|opening|start)\D{{0,20}}{_NUM}\s*{_UNIT}"
)
# с N по M / from N to M (seconds assumed if no unit on both — optional)
_RANGE_RE = re.compile(
    rf"(?i)(?:с|from)\s*{_NUM}\s*(?:с(?:ек)?|s|sec)?\s*"
    rf"(?:по|до|to|-|–|—)\s*{_NUM}\s*(?:с(?:ек)?|s|sec|м(?:ин)?|min)?"
)


def _to_seconds(value: str, unit: str) -> float:
    num = float(value.replace(",", "."))
    u = unit.lower()
    if u in {"m", "м"} or u.startswith("min") or u.startswith("мин"):
        return num * 60.0
    return num


def _extract_seconds(match: re.Match[str]) -> float:
    raw = match.group(0)
    num_m = re.search(_NUM, raw)
    unit_m = re.search(_UNIT, raw, flags=re.I)
    if not num_m:
        return 0.0
    unit = unit_m.group(0) if unit_m else "s"
    return _to_seconds(num_m.group(1), unit)


def parse_analysis_window(
    prompt: str,
    *,
    duration_sec: float,
) -> AnalysisWindow | None:
    """
    If the user asks for first/last N seconds (or a range), return that window.

    Vision models only see a single frame — they cannot know “last 30s” unless
    we hard-restrict sampling to that segment.
    """
    if duration_sec <= 0 or not (prompt or "").strip():
        return None

    text = prompt.strip()

    last = _LAST_RE.search(text)
    if last:
        seconds = min(duration_sec, max(1.0, _extract_seconds(last)))
        start = max(0.0, duration_sec - seconds)
        return AnalysisWindow(
            start_sec=round(start, 3),
            end_sec=round(duration_sec, 3),
            label=f"последние {seconds:.0f}с ({start:.1f}–{duration_sec:.1f}с)",
        )

    first = _FIRST_RE.search(text)
    if first:
        seconds = min(duration_sec, max(1.0, _extract_seconds(first)))
        return AnalysisWindow(
            start_sec=0.0,
            end_sec=round(seconds, 3),
            label=f"первые {seconds:.0f}с (0–{seconds:.1f}с)",
        )

    rng = _RANGE_RE.search(text)
    if rng:
        a = float(rng.group(1).replace(",", "."))
        b = float(rng.group(2).replace(",", "."))
        start, end = sorted((a, b))
        start = max(0.0, min(start, duration_sec))
        end = max(start + 0.5, min(end, duration_sec))
        return AnalysisWindow(
            start_sec=round(start, 3),
            end_sec=round(end, 3),
            label=f"диапазон {start:.1f}–{end:.1f}с",
        )

    return None
