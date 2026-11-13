"""Chunk splitting for long-video progressive processing."""

from bot.pipeline import _iter_chunks


def test_short_range_is_single_chunk() -> None:
    assert _iter_chunks(0.0, 90.0, 120.0) == [(0.0, 90.0)]
    assert _iter_chunks(0.0, 140.0, 120.0) == [(0.0, 140.0)]


def test_long_range_splits_evenly() -> None:
    chunks = _iter_chunks(0.0, 480.0, 120.0)
    assert len(chunks) == 4
    assert chunks[0][0] == 0.0
    assert chunks[-1][1] == 480.0
    for start, end in chunks:
        assert end > start


def test_analysis_window_chunked() -> None:
    chunks = _iter_chunks(60.0, 420.0, 120.0)
    assert len(chunks) >= 2
    assert chunks[0][0] == 60.0
    assert chunks[-1][1] == 420.0
