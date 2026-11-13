from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = ""
    runware_api_key: str = ""

    # MVP limits
    max_file_bytes: int = 20 * 1024 * 1024  # Telegram Bot API getFile limit
    max_duration_sec: float = 180.0
    frame_interval_sec: float = 2.0
    max_frames: int = 30
    top_clips: int = 3
    clip_min_sec: float = 5.0
    clip_max_sec: float = 12.0
    min_interesting_score: float = 5.0
    # Softmax temperature for randomizing among high-scoring moments (lower = greedier)
    selection_temperature: float = 1.2
    # Candidate pool size before weighted sampling (must be >= top_clips)
    selection_pool_size: int = 8

    # Qwen2.5-VL-3B-Instruct — cheapest caption model on Runware
    runware_model: str = "runware:152@1"

    # Default “what is interesting” criteria (user can override per video)
    default_interest_prompt: str = (
        "action, emotion, surprise, visual change, comedy, or a key story moment"
    )
    max_interest_prompt_chars: int = 500

    # yt-dlp auth for TikTok (and sometimes YouTube)
    # Prefer cookies file; browser fallback: chrome / firefox / chromium / brave / edge
    ytdlp_cookies_file: str = ""
    ytdlp_cookies_from_browser: str = ""

    # Persistent cache of downloaded social videos (skip re-download)
    video_cache_dir: str = ".cache/videos"


settings = Settings()
