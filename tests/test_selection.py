"""Unit tests for score/LLM clip parsing and window selection."""

from pathlib import Path

from bot.formats import TIKTOK_VF, OutputFormat
from bot.pipeline import _vf_for_format
from bot.runware_client import (
    DescribedFrame,
    ScoredFrame,
    build_rank_prompt,
    parse_llm_clips_response,
    parse_score_response,
    select_top_windows,
)


def test_parse_score_json() -> None:
    score, reason = parse_score_response(
        '{"score": 8, "reason": "резкий поворот камеры"}'
    )
    assert score == 8.0
    assert "поворот" in reason


def test_parse_score_wrapped() -> None:
    score, reason = parse_score_response(
        'Sure:\n```json\n{"score": 9.5, "reason": "кульминация"}\n```'
    )
    assert score == 9.5
    assert reason == "кульминация"


def test_parse_score_invalid() -> None:
    score, reason = parse_score_response("no json here")
    assert score == 0.0
    assert reason == ""


def test_parse_llm_clips_response() -> None:
    text = """
    {
      "clips": [
        {"start": 60.0, "end": 70.0, "score": 9, "reason": "финал", "peak": 65.0},
        {"start": 62.0, "end": 72.0, "score": 8, "reason": "повтор", "peak": 66.0}
      ]
    }
    """
    windows = parse_llm_clips_response(
        text,
        duration_sec=90.0,
        top_n=3,
        clip_min_sec=5.0,
        clip_max_sec=12.0,
        range_start_sec=60.0,
        range_end_sec=90.0,
    )
    assert len(windows) == 1  # second overlaps first
    assert windows[0].start_sec >= 60.0
    assert windows[0].end_sec <= 90.0


def test_build_rank_prompt_includes_criteria_and_timeline() -> None:
    frames = [
        DescribedFrame(10.0, "a man speaks on stage", Path("a.jpg")),
        DescribedFrame(20.0, "crowd cheers", Path("b.jpg")),
    ]
    prompt = build_rank_prompt(
        interest_criteria="смешные реакции",
        duration_sec=90.0,
        frames=frames,
        top_n=3,
        clip_min_sec=5.0,
        clip_max_sec=12.0,
        range_start_sec=0.0,
        range_end_sec=90.0,
    )
    assert "смешные реакции" in prompt
    assert "[10.0s] a man speaks on stage" in prompt
    assert "crowd cheers" in prompt


def test_vf_for_format() -> None:
    assert _vf_for_format(OutputFormat.ORIGINAL) is None
    assert _vf_for_format(OutputFormat.TIKTOK) == TIKTOK_VF
    assert "1080:1920" in TIKTOK_VF


def test_select_top_windows_non_overlapping() -> None:
    frames = [
        ScoredFrame(2.0, 9.0, "a", Path("a.jpg")),
        ScoredFrame(4.0, 8.5, "b", Path("b.jpg")),
        ScoredFrame(40.0, 7.0, "c", Path("c.jpg")),
        ScoredFrame(80.0, 6.0, "d", Path("d.jpg")),
        ScoredFrame(100.0, 3.0, "e", Path("e.jpg")),
    ]
    windows = select_top_windows(
        frames,
        duration_sec=120.0,
        top_n=3,
        clip_min_sec=5.0,
        clip_max_sec=12.0,
        min_score=5.0,
        frame_interval_sec=2.0,
        temperature=0.05,
        pool_size=8,
    )
    assert len(windows) == 3
    assert windows[0].start_sec < windows[1].start_sec < windows[2].start_sec
    for i in range(len(windows) - 1):
        assert windows[i].end_sec <= windows[i + 1].start_sec


def test_select_top_windows_varies_with_temperature() -> None:
    frames = [
        ScoredFrame(float(i * 20), 7.0 + (i % 3) * 0.1, "x", Path(f"{i}.jpg"))
        for i in range(8)
    ]
    seen: set[tuple[float, ...]] = set()
    for _ in range(30):
        windows = select_top_windows(
            frames,
            duration_sec=200.0,
            top_n=3,
            clip_min_sec=5.0,
            clip_max_sec=12.0,
            min_score=5.0,
            frame_interval_sec=2.0,
            temperature=2.0,
            pool_size=8,
        )
        seen.add(tuple(sorted(w.peak_sec for w in windows)))
    assert len(seen) >= 2
