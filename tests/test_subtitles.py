"""ASS cue building / word chunking (no Whisper in CI)."""

from bot.subtitles import (
    SubtitleCue,
    TimedWord,
    _ass_timestamp,
    _escape_ass_text,
    build_ass_document,
    chunk_words,
)


def test_chunk_words_groups_by_max() -> None:
    words = [
        TimedWord(0.0, 0.3, "hello"),
        TimedWord(0.3, 0.5, "beautiful"),
        TimedWord(0.5, 0.8, "world"),
        TimedWord(0.9, 1.1, "again"),
    ]
    cues = chunk_words(words, max_words=3)
    assert len(cues) == 2
    assert cues[0].text == "hello beautiful world"
    assert cues[0].start_sec == 0.0
    assert cues[0].end_sec == 0.8
    assert cues[1].text == "again"


def test_chunk_words_skips_empty() -> None:
    words = [
        TimedWord(0.0, 0.2, "  "),
        TimedWord(0.2, 0.4, "ok"),
    ]
    cues = chunk_words(words, max_words=3)
    assert len(cues) == 1
    assert cues[0].text == "ok"


def test_ass_timestamp() -> None:
    assert _ass_timestamp(0) == "0:00:00.00"
    assert _ass_timestamp(65.42) == "0:01:05.42"


def test_escape_ass_text() -> None:
    assert _escape_ass_text("a{b}c\\d") == "a\\{b\\}c\\\\d"


def test_build_ass_document_contains_dialogue() -> None:
    doc = build_ass_document(
        [
            SubtitleCue(0.0, 1.2, "hello world"),
            SubtitleCue(1.2, 2.0, "again"),
        ]
    )
    assert "[Script Info]" in doc
    assert "Style: Default," in doc
    assert "Dialogue: 0,0:00:00.00,0:00:01.20,Default,,0,0,0,,HELLO WORLD" in doc
    assert "AGAIN" in doc
