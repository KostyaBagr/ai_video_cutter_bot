"""Tests for temporal prompt parsing."""

from bot.time_window import parse_analysis_window


def test_last_30_seconds() -> None:
    w = parse_analysis_window("только последние 30 сек видео", duration_sec=90.0)
    assert w is not None
    assert w.start_sec == 60.0
    assert w.end_sec == 90.0


def test_first_20_seconds() -> None:
    w = parse_analysis_window("первые 20 секунд", duration_sec=100.0)
    assert w is not None
    assert w.start_sec == 0.0
    assert w.end_sec == 20.0


def test_last_english() -> None:
    w = parse_analysis_window("last 15 seconds funny moments", duration_sec=60.0)
    assert w is not None
    assert w.start_sec == 45.0
    assert w.end_sec == 60.0


def test_no_time_window() -> None:
    assert parse_analysis_window("смешные реакции", duration_sec=90.0) is None


def test_last_longer_than_video() -> None:
    w = parse_analysis_window("последние 120 сек", duration_sec=40.0)
    assert w is not None
    assert w.start_sec == 0.0
    assert w.end_sec == 40.0
