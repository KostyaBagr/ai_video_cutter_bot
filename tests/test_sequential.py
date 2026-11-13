"""Sequential equal-length cutting windows."""

from bot.pipeline import sequential_windows


def test_sequential_exact_split() -> None:
    windows = sequential_windows(60.0, 6.0)
    assert len(windows) == 10
    assert windows[0].start_sec == 0.0
    assert windows[0].end_sec == 6.0
    assert windows[-1].start_sec == 54.0
    assert windows[-1].end_sec == 60.0
    for i in range(1, len(windows)):
        assert windows[i].start_sec == windows[i - 1].end_sec


def test_sequential_remainder_included() -> None:
    windows = sequential_windows(25.0, 10.0)
    assert len(windows) == 3
    assert windows[-1].start_sec == 20.0
    assert windows[-1].end_sec == 25.0


def test_sequential_respects_max_clips() -> None:
    windows = sequential_windows(100.0, 5.0, max_clips=3)
    assert len(windows) == 3
    assert windows[-1].end_sec == 15.0


def test_sequential_empty_on_bad_input() -> None:
    assert sequential_windows(0.0, 6.0) == []
    assert sequential_windows(10.0, 0.0) == []
