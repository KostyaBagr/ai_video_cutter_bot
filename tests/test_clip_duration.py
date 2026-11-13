"""Clip duration parsing and pipeline bounds."""

from bot.handlers import _parse_clip_duration
from bot.pipeline import _clip_bounds


def test_parse_clip_duration_plain_number() -> None:
    assert _parse_clip_duration("15") == 15.0
    assert _parse_clip_duration("8с") == 8.0
    assert _parse_clip_duration("20 сек") == 20.0
    assert _parse_clip_duration("12 sec") == 12.0


def test_parse_clip_duration_rejects_out_of_range() -> None:
    assert _parse_clip_duration("2") is None
    assert _parse_clip_duration("90") is None
    assert _parse_clip_duration("abc") is None
    assert _parse_clip_duration("") is None


def test_clip_bounds_default() -> None:
    lo, hi = _clip_bounds(None)
    assert lo == 5.0
    assert hi == 12.0


def test_clip_bounds_user_choice_with_slack() -> None:
    lo, hi = _clip_bounds(15.0)
    assert lo == 14.0
    assert hi == 16.0


def test_clip_bounds_clamped_to_global_limits() -> None:
    lo, hi = _clip_bounds(3.0)
    assert lo == 3.0
    assert hi == 4.0

    lo, hi = _clip_bounds(60.0)
    assert lo == 59.0
    assert hi == 60.0
