# YouTube Shorts Uploader

Telegram bot pipeline for publishing YouTube Shorts. Three flows:

1. **`/twitter`** — download a video from an X/Twitter post, generate metadata with Gemini, review, publish.
2. **`/hugging_face`** — invent a Midnight Souls scene with Gemini, generate vertical HD video via Hugging Face Inference Providers, review the video, then metadata, publish.
3. **`/pixabay`** — pick random tags from a predefined library, download a vertical HD Pixabay video (≤60s, no re-encode), review video then metadata, publish.

## Flows

### Twitter

1. Admin sends `/twitter`, then an X/Twitter post URL.
2. Server clears `storage/videos/` and downloads the video there.
3. Server sends the video to Gemini and receives JSON metadata (`title`, `description`, `viral_title_tags`, `shorts_tags`).
4. Bot returns formatted JSON with **Approve**, **Decline**, **Modify**.
5. **Approve** uploads to YouTube Shorts, then deletes the local video.
6. **Decline** deletes the local video and stops.
7. **Modify** asks for edited JSON; bot re-shows the review keyboard.

### Hugging Face (Midnight Souls)

1. Admin sends `/hugging_face` (starts immediately — no topic input).
2. Gemini invents a cozy ambient scene and 2–4 continuity-aware clip prompts for the channel brand.
3. Hugging Face generates clips (text-to-video, then image-to-video from the last frame when possible).
4. ffmpeg merges/normalizes to **1080×1920**, **8–15 seconds**, H.264 mp4.
5. Bot sends the video to Telegram with **Approve** / **Decline** / **Modify**.
6. **Approve** → Gemini metadata JSON + second review keyboard.
7. **Modify** (video stage) → regenerate a new video.
8. **Modify** (metadata stage) → paste edited JSON (same as Twitter).
9. **Approve** (metadata) → YouTube upload; **Decline** → delete and stop.

### Pixabay (Midnight Souls stock)

1. Admin sends `/pixabay` → confirmation with **Start** / **Back to menu** (safe for misclicks).
2. **Start** → bot picks 3–4 random tags from the predefined library as the Pixabay search query.
3. Pixabay Video API search; bot picks an unused **vertical HD** film clip with duration **1–60s**.
4. Downloads the highest-resolution vertical stream **as-is** (no ffmpeg re-encode).
5. Bot sends the video with Pixabay attribution and **Approve** / **Decline** / **Modify**.
6. **Approve** → Gemini metadata JSON + second review (same as Twitter/HF).
7. **Modify** (video) → next unused hit for the same query; if none left, pick a new tag set.
8. **Decline** at either stage → delete and stop.
9. **Approve** (metadata) → YouTube upload on the same OAuth channel.

## Requirements

- Python 3.12+
- [ffmpeg](https://ffmpeg.org/) (yt-dlp merge + HF clip merge/normalize)
- Telegram bot token
- Gemini API key (Google AI Studio)
- Hugging Face token with Inference Providers permission (`HF_TOKEN`) — for `/hugging_face`
- Pixabay API key (`PIXABAY_API_KEY`) — for `/pixabay`
- Google Cloud project with YouTube Data API v3 enabled
- OAuth client credentials for desktop/installed app

## Setup

### 1. Clone and install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

On Linux/macOS, activate with `source .venv/bin/activate`.

Install ffmpeg if it is not already available:

```bash
# Windows (winget)
winget install ffmpeg

# macOS
brew install ffmpeg

# Debian/Ubuntu
sudo apt install ffmpeg
```

### 2. Configure environment

```bash
copy .env.example .env
```

Edit `.env` with your values. See [Environment variables](#environment-variables) for the full list.

### 3. Telegram bot

1. Create a bot with [@BotFather](https://t.me/BotFather).
2. Put the token in `TELEGRAM_BOT_TOKEN`.
3. Get your chat ID (message [@userinfobot](https://t.me/userinfobot) or inspect bot updates) and set `ADMIN_CHAT_ID`.

### 4. Gemini API

1. Create an API key in [Google AI Studio](https://aistudio.google.com/).
2. Set `GEMINI_API_KEY`.
3. Optionally set `GEMINI_MODEL` (default: `gemini-3.5-flash`).

### 5. Hugging Face

1. Create an access token at [Hugging Face settings](https://huggingface.co/settings/tokens) with Inference Providers permission.
2. Set `HF_TOKEN`.
3. Optionally override `HF_VIDEO_MODEL`, `HF_I2V_MODEL`, `HF_PROVIDER`, and `HF_TARGET_DURATION_SECONDS`.

### 6. Pixabay API

1. Open [Pixabay API docs](https://pixabay.com/api/docs/).
2. Sign up or log in to Pixabay.
3. On that page, your personal API key appears in the `key` parameter section.
4. Set `PIXABAY_API_KEY` in `.env`.
5. Restart the bot. `/pixabay` refuses to start if the key is missing.

### 7. YouTube OAuth

1. In [Google Cloud Console](https://console.cloud.google.com/), enable **YouTube Data API v3**.
2. Create OAuth credentials for a **Desktop app**.
3. Download the JSON file and save it as `secrets/client_secret.json`.
4. On first upload, the app opens a browser for one-time OAuth. The refresh token is saved to `secrets/youtube_token.json`.

For Docker or headless deployment, generate `secrets/youtube_token.json` locally first, then provide it to the container (see [Docker](#docker)).

### 8. Run locally

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Health check: `GET http://localhost:8000/health`

## Docker

Build and run with a `.env` file:

```bash
docker build -t youtube-shorts-uploader .
docker run --env-file .env -p 7860:7860 youtube-shorts-uploader
```

Health check: `GET http://localhost:7860/health`

The container listens on port **7860** and includes ffmpeg. OAuth credential files can be supplied in two ways:

**Option A — bind-mount local secrets:**

```bash
docker run --env-file .env \
  -v ./secrets:/app/secrets \
  -p 7860:7860 youtube-shorts-uploader
```

**Option B — inject JSON via environment variables** (useful on platforms without persistent volumes):

```bash
docker run --env-file .env \
  -e YOUTUBE_CLIENT_SECRETS_JSON='{"installed":{...}}' \
  -e YOUTUBE_TOKEN_JSON='{"token":"...","refresh_token":"..."}' \
  -p 7860:7860 youtube-shorts-uploader
```

The entrypoint writes these variables to `secrets/client_secret.json` and `secrets/youtube_token.json` at startup. Browser-based OAuth does not work inside the container, so create the token file locally before deploying.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | yes | — | Bot token from @BotFather |
| `ADMIN_CHAT_ID` | yes | — | Telegram chat ID allowed to use the bot |
| `GEMINI_API_KEY` | yes | — | Google AI Studio API key |
| `GEMINI_MODEL` | no | `gemini-3.5-flash` | Gemini model for prompts/metadata |
| `HF_TOKEN` | for `/hugging_face` | — | Hugging Face token (Inference Providers) |
| `HF_VIDEO_MODEL` | no | `Wan-AI/Wan2.2-TI2V-5B` | Text-to-video model |
| `HF_I2V_MODEL` | no | `Wan-AI/Wan2.2-I2V-A14B` | Image-to-video continuity model |
| `HF_PROVIDER` | no | `auto` | Inference provider routing |
| `HF_TARGET_DURATION_SECONDS` | no | `12` | Preferred Shorts length (8–15) |
| `PIXABAY_API_KEY` | for `/pixabay` | — | Pixabay API key from [api docs](https://pixabay.com/api/docs/) |
| `YOUTUBE_CLIENT_SECRETS_FILE` | no | `secrets/client_secret.json` | Path to OAuth client JSON |
| `YOUTUBE_TOKEN_FILE` | no | `secrets/youtube_token.json` | Path to saved OAuth token |
| `YOUTUBE_PRIVACY_STATUS` | no | `private` | `private`, `public`, or `unlisted` |
| `YOUTUBE_CATEGORY_ID` | no | `22` | YouTube category (22 = People & Blogs) |
| `VIDEO_STORAGE_DIR` | no | `storage/videos` | Temporary video directory |
| `SESSION_TTL_HOURS` | no | `24` | Hours before stale pending jobs are removed on startup |
| `YOUTUBE_CLIENT_SECRETS_JSON` | no | — | Docker: inline OAuth client JSON |
| `YOUTUBE_TOKEN_JSON` | no | — | Docker: inline OAuth token JSON |

## Usage

1. Start a chat with your bot and send `/start`.
2. Use `/twitter` with an X/Twitter URL, `/hugging_face` for an AI Short, or `/pixabay` for a stock Short.
3. Review video (HF/Pixabay) and/or metadata JSON, then choose an action.

Modify metadata JSON shape:

```json
{
  "title": "My updated title",
  "description": "Updated description",
  "viral_title_tags": ["viral", "shorts", "trend"],
  "shorts_tags": ["shorts", "viral", "trend", "youtube"]
}
```

Only the four metadata fields above are required when modifying; `display_title` is computed automatically.

## Project structure

```
app/
  main.py              # FastAPI app + Telegram bot lifecycle
  bot.py               # Telegram handlers and review flows
  config.py            # Settings from environment
  schemas.py           # Pydantic models
  session_store.py     # In-memory job state
  prompts/
    midnight_souls.py  # Channel brand brief for HF video prompts
  data/
    pixabay_tags.py    # Predefined Pixabay search tags
  services/
    twitter_downloader.py
    pixabay_client.py
    gemini_client.py
    huggingface_video.py
    video_pipeline.py
    ffmpeg_utils.py
    youtube_uploader.py
    cleanup.py
  utils/
    metadata_rules.py
storage/videos/        # Temporary videos
secrets/               # OAuth credentials (not committed)
Dockerfile
entrypoint.sh
```

## Notes

- Only the configured `ADMIN_CHAT_ID` can use the bot.
- Session state is in-memory only; restarting the server clears pending jobs.
- Stale pending sessions and videos older than `SESSION_TTL_HOURS` are cleaned on startup.
- Starting a new Twitter, HF, or Pixabay job clears leftover files in `storage/videos/`.
- Pixabay videos are downloaded without re-encoding to preserve quality; only already-vertical HD clips are used.
- Default YouTube privacy is `private`; change `YOUTUBE_PRIVACY_STATUS` to `public` or `unlisted` if needed.
- Hugging Face free-tier clips are often short; the pipeline merges 2–4 continuity clips to reach 8–15 seconds.
