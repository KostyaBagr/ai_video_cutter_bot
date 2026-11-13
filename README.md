# AI Video Cutter Bot (MVP)

Telegram-бот: присылаешь ссылку на YouTube / TikTok → бот находит до **3 самых интересных моментов** через Runware (Qwen2.5-VL-3B) и режет отдельные клипы локально через **ffmpeg** (без AI-нарезки). MP4-файлы не принимаются.

Скачивание ссылок: [yt-dlp](https://github.com/yt-dlp/yt-dlp). Длительность проверяется до/после скачивания (лимит MVP — 600 сек / 10 мин).

## Как это работает

1. Скачивает видео по ссылке YouTube / TikTok (повторные ссылки берутся из `.cache/videos/`)
2. Спрашивает **промпт**: что считать интересным (или «По умолчанию»)
3. Спрашивает **длительность клипа**: кнопки 5–30с, своё число 3–60с, или «По умолчанию (5–12с)»
4. Спрашивает формат: **оригинал** или **TikTok 9:16** (вертикальный center-crop 1080×1920)
5. Спрашивает **субтитры**: Да / Нет (локальный Whisper, burn-in TikTok-стиль)
6. Длинные ролики анализирует частями (~2 мин), затем режет клипы и **шлёт по мере готовности**
7. Достаёт кадры (интервал ~2с, до 90 кадров суммарно) через ffmpeg
8. **Stage 1:** дешёвый VL (`runware:152@1`) только описывает кадры
9. **Stage 2:** текстовая LLM (`deepseek:v4@flash`) выбирает моменты по таймлайну части + твоему промпту
10. Режет до 3 клипов выбранной длины через ffmpeg (с crop при TikTok); при «Да» — накладывает субтитры

## Требования

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/) в `PATH` (нужны `ffmpeg` и `ffprobe`)
- Токен Telegram-бота и API-ключ [Runware](https://runware.ai/)

```bash
# Ubuntu/Debian
sudo apt install ffmpeg
```

## Установка и запуск

```bash
cd ai_video_cutter_bot

# venv (через uv или python -m venv)
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt

cp .env.example .env
# заполните TELEGRAM_BOT_TOKEN и RUNWARE_API_KEY

python -m bot.main
```

Проверка юнит-тестов:

```bash
uv pip install pytest pytest-xdist
pytest tests -n 2
```

## Docker

```bash
cp .env.example .env   # заполните токены
# cookies.txt рядом с Dockerfile — копируется в образ при build

docker compose up -d --build
docker compose logs -f bot
```

Образ: Python 3.12 + ffmpeg + DejaVu + faster-whisper + `cookies.txt`. Тома: SQLite (`.data`) и кэш (видео + HuggingFace). `YTDLP_COOKIES_FROM_BROWSER` в Docker не используйте.

## Лимиты MVP

- Только ссылки YouTube / TikTok (без загрузки MP4)
- Длительность видео: до 10 минут
- Длительность клипа: по умолчанию 5–12с, либо выбранная (3–60с)
- Опциональные burn-in субтитры: `faster-whisper` (`base`, CPU); модель скачивается при первом запуске (~150MB)
- Баланс в алмазах (старт 50 💎): AI 20, нарезка подряд 5, субтитры +3 за клип
- Магазин `/shop` через ЮKassa (Telegram Payments): S 50💎=69₽, M 150💎=149₽, L 500💎=399₽ (RUB min в Telegram — 65₽)
- Результат: до 3 клипов (AI) или до 40 кусков (нарезка подряд)

## Переменные окружения

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен от [@BotFather](https://t.me/BotFather) |
| `RUNWARE_API_KEY` | Ключ с [runware.ai](https://runware.ai/) |
| `APP_ENV` | `prod` (без ограничений) или `debug` (только `DEBUG_TELEGRAM_ID`) |
| `DEBUG_TELEGRAM_ID` | Telegram user id, которому можно писать боту в `debug` |
| `YOOKASSA_PROVIDER_TOKEN` | Токен из BotFather → Payments → ЮKassa ([дока](https://yookassa.ru/docs/support/payments/onboarding/integration/cms-module/telegram)) |
| `YTDLP_COOKIES_FILE` | Путь к `cookies.txt` (Netscape) — нужен для TikTok |
| `YTDLP_COOKIES_FROM_BROWSER` | Альтернатива: `chrome` / `firefox` / `chromium` / … |

### TikTok: cookies (обязательно, если пишет «requiring login»)

1. Залогинься в TikTok в браузере.
2. Экспортируй cookies расширением вроде **Get cookies.txt LOCALLY** (формат Netscape).
3. Положи файл в корень проекта как `cookies.txt` (он в `.gitignore`, но копируется в Docker-образ при `docker compose build`).
4. Локально в `.env`: `YTDLP_COOKIES_FILE=./cookies.txt`. В Docker путь уже `/app/cookies.txt`.

Подробнее: [yt-dlp FAQ — cookies](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)
