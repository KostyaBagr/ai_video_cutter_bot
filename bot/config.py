from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = ""
    runware_api_key: str = ""

    # debug | prod — in debug only DEBUG_TELEGRAM_ID may use the bot
    app_env: str = "prod"
    debug_telegram_id: int = 0

    @property
    def is_debug(self) -> bool:
        return self.app_env.strip().lower() in {"debug", "dev", "development"}

    # MVP limits
    max_file_bytes: int = 20 * 1024 * 1024  # Telegram Bot API getFile limit
    max_duration_sec: float = 600.0  # 10 minutes
    frame_interval_sec: float = 2.0
    max_frames: int = 90
    # Long videos are processed in time chunks; clips are sent as each chunk finishes
    analysis_chunk_sec: float = 120.0
    top_clips: int = 3
    # Max pieces when cutting the whole video into equal sequential clips
    sequential_max_clips: int = 40
    # Default piece length for sequential mode when user picks "auto"
    sequential_default_clip_sec: float = 6.0
    # Default clip length when user doesn't choose
    clip_min_sec: float = 5.0
    clip_max_sec: float = 12.0
    # Allowed user-selectable clip lengths (seconds)
    clip_duration_choices: tuple[int, ...] = (5, 8, 10, 15, 20, 30)
    clip_duration_min_sec: float = 3.0
    clip_duration_max_sec: float = 60.0
    min_interesting_score: float = 5.0
    # Softmax temperature for randomizing among high-scoring moments (lower = greedier)
    selection_temperature: float = 1.2
    # Candidate pool size before weighted sampling (must be >= top_clips)
    selection_pool_size: int = 8

    # Stage 1: cheap VL captions (Qwen2.5-VL-3B)
    runware_caption_model: str = "runware:152@1"
    # Stage 2: text LLM ranks timeline vs user prompt (DeepSeek-V4-Flash)
    runware_llm_model: str = "deepseek:v4@flash"
    runware_chat_base_url: str = "https://api.runware.ai/v1"

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

    # Users / diamonds
    sqlite_path: str = ".data/bot.db"
    starter_diamonds: int = 50
    price_sequential_base: int = 5
    price_ai_base: int = 20
    price_subtitles_per_clip: int = 3

    # YooKassa via Telegram Payments (token from @BotFather → Payments)
    yookassa_provider_token: str = ""

    # Burn-in subtitles (local faster-whisper)
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    subtitle_max_words: int = 3
    subtitle_font_name: str = "DejaVu Sans"
    subtitle_font_size: int = 56
    subtitle_margin_v: int = 80


settings = Settings()
