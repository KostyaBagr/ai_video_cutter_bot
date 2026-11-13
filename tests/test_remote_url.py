"""URL parsing for YouTube / TikTok links."""

from bot.remote_download import cache_filename, detect_source, extract_media_url


def test_extract_youtube_watch() -> None:
    text = "смотри https://www.youtube.com/watch?v=dQw4w9WgXcQ круто"
    assert extract_media_url(text) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert detect_source(extract_media_url(text) or "") == "youtube"


def test_extract_youtu_be() -> None:
    url = extract_media_url("https://youtu.be/dQw4w9WgXcQ")
    assert url == "https://youtu.be/dQw4w9WgXcQ"


def test_extract_youtube_shorts() -> None:
    url = extract_media_url("https://youtube.com/shorts/abc123XYZ00")
    assert url is not None
    assert "shorts" in url


def test_extract_tiktok() -> None:
    url = extract_media_url(
        "https://www.tiktok.com/@user/video/7123456789012345678"
    )
    assert url is not None
    assert detect_source(url) == "tiktok"


def test_extract_tiktok_short() -> None:
    url = extract_media_url("https://vm.tiktok.com/ZMabcdefg/")
    assert url is not None
    assert detect_source(url) == "tiktok"


def test_reject_unsupported() -> None:
    assert extract_media_url("https://vimeo.com/123") is None
    assert extract_media_url("просто текст") is None


def test_reject_lookalike_hosts() -> None:
    assert detect_source("https://notyoutube.com/watch?v=dQw4w9WgXcQ") is None
    assert detect_source("https://evil-youtube.com/watch?v=dQw4w9WgXcQ") is None
    assert detect_source("https://tiktok.com.evil.example/video/1") is None


def test_cache_filename() -> None:
    assert cache_filename("youtube", "dQw4w9WgXcQ") == "youtube_dQw4w9WgXcQ.mp4"
    assert cache_filename("tiktok", "7123456789012345678") == (
        "tiktok_7123456789012345678.mp4"
    )
    assert "/" not in cache_filename("youtube", "a/b?c")
    assert ".." not in cache_filename("youtube", "../etc/passwd")
