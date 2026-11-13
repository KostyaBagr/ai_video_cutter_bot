"""Download remote videos via yt-dlp (YouTube, TikTok, …) with disk cache."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL

from bot.config import settings

logger = logging.getLogger(__name__)

_YOUTUBE_RE = re.compile(
    r"(?i)\bhttps?://(?:www\.)?(?:"
    r"youtube\.com/(?:watch\?[^\s]*v=|shorts/|embed/|live/)|"
    r"youtu\.be/"
    r")[^\s]+"
)

_TIKTOK_RE = re.compile(
    r"(?i)\bhttps?://(?:(?:www|vm|vt|m)\.)?tiktok\.com/[^\s]+"
)

_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)
_TIKTOK_HOSTS = frozenset(
    {
        "tiktok.com",
        "www.tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
        "m.tiktok.com",
    }
)


class RemoteDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    from_cache: bool
    video_id: str
    source: str


def sanitize_id(raw: str) -> str:
    """Safe path component for cache / staging (no traversal)."""
    safe = _SAFE_ID_RE.sub("_", (raw or "").strip()).strip("_")[:80]
    return safe or "unknown"


def _hostname(url: str) -> str:
    host = urlparse(url).hostname
    return (host or "").lower()


def detect_source(url: str) -> str | None:
    """Strict host allowlist (no substring lookalike domains)."""
    host = _hostname(url)
    if not host:
        return None
    if host in _YOUTUBE_HOSTS or host.endswith(".youtube.com"):
        return "youtube"
    if host in _TIKTOK_HOSTS or host.endswith(".tiktok.com"):
        return "tiktok"
    return None


def extract_media_url(text: str) -> str | None:
    """Extract first supported YouTube or TikTok URL from text."""
    if not text:
        return None
    for pattern in (_YOUTUBE_RE, _TIKTOK_RE):
        match = pattern.search(text.strip())
        if match:
            url = match.group(0).rstrip(").,]}>\"'")
            if detect_source(url):
                return url
    return None


def cache_filename(source: str, video_id: str) -> str:
    return f"{sanitize_id(source)}_{sanitize_id(video_id)}.mp4"


def _url_fallback_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _youtube_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        return parsed.path.lstrip("/").split("/")[0] or None
    query_id = parse_qs(parsed.query).get("v", [None])[0]
    if query_id:
        return query_id
    match = re.match(r"/(?:shorts|embed|live|v)/([^/?#]+)", parsed.path)
    return match.group(1) if match else None


def _tiktok_id_from_url(url: str) -> str | None:
    match = re.search(r"/video/(\d+)", url)
    return match.group(1) if match else None


def _cookie_opts() -> dict:
    """Attach cookies for TikTok login wall (file preferred over browser)."""
    cookie_file = (settings.ytdlp_cookies_file or "").strip()
    if cookie_file:
        path = Path(cookie_file).expanduser()
        if not path.is_file():
            logger.error("Configured cookies file missing: %s", path)
            raise RemoteDownloadError(
                "Не получилось скачать это видео.\n"
                "Попробуй другую ссылку или зайди позже."
            )
        return {"cookiefile": str(path)}

    browser = (settings.ytdlp_cookies_from_browser or "").strip()
    if browser:
        return {"cookiesfrombrowser": (browser,)}

    return {}


def _base_opts(**extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "remote_components": ["ejs:github"],
    }
    opts.update(_cookie_opts())
    opts.update(extra)
    return opts


def _friendly_download_error(exc: Exception, source: str) -> str:
    msg = str(exc)
    logger.warning("Download failed (%s): %s", source, msg)
    if "requiring login" in msg.lower() or "cookies" in msg.lower():
        return (
            "Не получилось скачать это видео.\n"
            "Попробуй другую ссылку или зайди позже."
        )
    return "Не получилось скачать видео. Проверь ссылку или попробуй другое."


def _probe_info(url: str) -> dict:
    opts = _base_opts(skip_download=True)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RemoteDownloadError("Не удалось получить информацию о видео.")
    if info.get("_type") == "playlist":
        raise RemoteDownloadError(
            "Нужна ссылка на одно видео, не на плейлист/канал."
        )
    return info


def _validate_duration(duration: float | None, *, stage: str) -> None:
    max_sec = settings.max_duration_sec
    if duration is None:
        if stage == "before":
            return
        raise RemoteDownloadError("Не удалось определить длительность видео.")
    if duration > max_sec:
        minutes = int(max_sec // 60)
        raise RemoteDownloadError(
            f"Это видео слишком длинное.\nМожно до {minutes} минут."
        )


def _cache_dir() -> Path:
    path = Path(settings.video_cache_dir).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_under_cache(path: Path) -> Path:
    cache = _cache_dir()
    resolved = path.resolve()
    if not resolved.is_relative_to(cache):
        raise RemoteDownloadError(
            "Не получилось скачать видео. Проверь ссылку или попробуй другое."
        )
    return resolved


def _cached_path(source: str, video_id: str) -> Path:
    return _ensure_under_cache(_cache_dir() / cache_filename(source, video_id))


def _is_usable_cache(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _resolve_video_id(url: str, source: str, info: dict) -> str:
    video_id = str(info.get("id") or "").strip()
    if not video_id:
        if source == "youtube":
            video_id = _youtube_id_from_url(url) or ""
        elif source == "tiktok":
            video_id = _tiktok_id_from_url(url) or ""
    return sanitize_id(video_id or _url_fallback_id(url))


def _download_sync(url: str) -> DownloadResult:
    source = detect_source(url)
    if not source:
        raise RemoteDownloadError("Поддерживаются только YouTube и TikTok.")

    info = _probe_info(url)
    _validate_duration(info.get("duration"), stage="before")
    video_id = _resolve_video_id(url, source, info)
    cached = _cached_path(source, video_id)

    if _is_usable_cache(cached):
        duration = _ffprobe_duration_sync(cached)
        _validate_duration(duration, stage="after")
        logger.info("Cache hit %s (%s)", cached.name, source)
        return DownloadResult(
            path=cached, from_cache=True, video_id=video_id, source=source
        )

    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".tmp_{sanitize_id(source)}_{sanitize_id(video_id)}_",
            dir=_cache_dir(),
        )
    )
    staging_dir = _ensure_under_cache(staging_dir)

    try:
        outtmpl = str(staging_dir / "input.%(ext)s")
        opts = _base_opts(
            format="bv*[height<=720]+ba/b[height<=720]/b",
            merge_output_format="mp4",
            outtmpl=outtmpl,
        )
        with YoutubeDL(opts) as ydl:
            ydl.download([url])

        video_path = _find_video_file(staging_dir)
        duration = _ffprobe_duration_sync(video_path)
        _validate_duration(duration, stage="after")

        if cached.exists():
            cached.unlink()
        shutil.move(str(video_path), str(cached))
        logger.info("Cached download %s (%s)", cached.name, source)
        return DownloadResult(
            path=cached, from_cache=False, video_id=video_id, source=source
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _ffprobe_duration_sync(video_path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        logger.error("ffprobe not found in PATH")
        raise RemoteDownloadError(
            "Не получилось обработать видео. Попробуй ещё раз позже."
        )
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RemoteDownloadError(
            "Не получилось обработать видео. Попробуй ещё раз позже."
        )
    data = json.loads(proc.stdout or "{}")
    return float(data.get("format", {}).get("duration") or 0)


def _find_video_file(directory: Path) -> Path:
    videos = sorted(
        [
            p
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not videos:
        logger.error("Download finished but video file missing in %s", directory)
        raise RemoteDownloadError(
            "Не получилось скачать видео. Проверь ссылку или попробуй другое."
        )
    return videos[0]


async def download_remote_video(url: str) -> DownloadResult:
    """Download (or reuse cache) a single remote video as mp4."""
    try:
        return await asyncio.to_thread(_download_sync, url)
    except RemoteDownloadError:
        raise
    except Exception as exc:
        logger.exception("Remote download failed")
        source = detect_source(url) or "remote"
        raise RemoteDownloadError(_friendly_download_error(exc, source)) from exc
