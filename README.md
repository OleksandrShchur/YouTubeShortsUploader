# YouTube Shorts Uploader

Telegram bot pipeline that downloads a video from an X/Twitter post, generates YouTube Shorts metadata with Gemini, and lets a single admin approve, decline, or modify before publishing to YouTube.

## Flow

1. Admin sends an X/Twitter post URL to the Telegram bot.
2. Server clears `storage/videos/` and downloads the video there.
3. Server sends the video to Gemini and receives JSON metadata:
   - `title`
   - `description`
   - `viral_title_tags` (3-4 tags appended to title)
   - `shorts_tags` (YouTube tags field)
4. Bot returns formatted JSON with buttons: **Approve**, **Decline**, **Modify**.
5. **Approve** uploads to YouTube Shorts immediately, then deletes the local video.
6. **Decline** deletes the local video and stops.
7. **Modify** asks the admin to paste edited JSON; bot validates it and sends updated JSON with the same buttons. The video stays on disk.

## Requirements

- Python 3.12+
- [ffmpeg](https://ffmpeg.org/) (used by yt-dlp to merge video and audio)
- Telegram bot token
- Gemini API key (Google AI Studio)
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

### 5. YouTube OAuth

1. In [Google Cloud Console](https://console.cloud.google.com/), enable **YouTube Data API v3**.
2. Create OAuth credentials for a **Desktop app**.
3. Download the JSON file and save it as `secrets/client_secret.json`.
4. On first upload, the app opens a browser for one-time OAuth. The refresh token is saved to `secrets/youtube_token.json`.

For Docker or headless deployment, generate `secrets/youtube_token.json` locally first, then provide it to the container (see [Docker](#docker)).

### 6. Run locally

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
  -p 7860:7860 \
  youtube-shorts-uploader
```

**Option B — inject JSON via environment variables** (useful on platforms without persistent volumes):

```bash
docker run --env-file .env \
  -e YOUTUBE_CLIENT_SECRETS_JSON='{"installed":{...}}' \
  -e YOUTUBE_TOKEN_JSON='{"token":"...","refresh_token":"..."}' \
  -p 7860:7860 \
  youtube-shorts-uploader
```

The entrypoint writes these variables to `secrets/client_secret.json` and `secrets/youtube_token.json` at startup. Browser-based OAuth does not work inside the container, so create the token file locally before deploying.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | yes | — | Bot token from @BotFather |
| `ADMIN_CHAT_ID` | yes | — | Telegram chat ID allowed to use the bot |
| `GEMINI_API_KEY` | yes | — | Google AI Studio API key |
| `GEMINI_MODEL` | no | `gemini-3.5-flash` | Gemini model for metadata generation |
| `YOUTUBE_CLIENT_SECRETS_FILE` | no | `secrets/client_secret.json` | Path to OAuth client JSON |
| `YOUTUBE_TOKEN_FILE` | no | `secrets/youtube_token.json` | Path to saved OAuth token |
| `YOUTUBE_PRIVACY_STATUS` | no | `private` | `private`, `public`, or `unlisted` |
| `YOUTUBE_CATEGORY_ID` | no | `22` | YouTube category (22 = People & Blogs) |
| `VIDEO_STORAGE_DIR` | no | `storage/videos` | Temporary video download directory |
| `SESSION_TTL_HOURS` | no | `24` | Hours before stale pending jobs are removed on startup |
| `YOUTUBE_CLIENT_SECRETS_JSON` | no | — | Docker: inline OAuth client JSON |
| `YOUTUBE_TOKEN_JSON` | no | — | Docker: inline OAuth token JSON |

## Usage

1. Start a chat with your bot and send `/start`.
2. Send an X/Twitter URL with a video, for example:
   `https://x.com/user/status/1234567890`
3. Wait for Gemini metadata JSON and review it. The response includes a `display_title` preview (title + viral hashtags).
4. Choose an action:
   - **Approve** → uploads to YouTube
   - **Decline** → cancels and deletes video
   - **Modify** → paste updated JSON like:

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
  bot.py               # Telegram handlers and review flow
  config.py            # Settings from environment
  schemas.py           # Pydantic models
  session_store.py     # In-memory job state
  services/
    twitter_downloader.py
    gemini_client.py
    youtube_uploader.py
    cleanup.py
  utils/
    metadata_rules.py
storage/videos/        # Temporary downloaded videos
secrets/               # OAuth credentials (not committed)
Dockerfile
entrypoint.sh
```

## Notes

- Only the configured `ADMIN_CHAT_ID` can use the bot.
- Session state is in-memory only; restarting the server clears pending jobs.
- Stale pending sessions and videos older than `SESSION_TTL_HOURS` are cleaned on startup.
- Starting a new URL clears any leftover files in `storage/videos/`.
- Default YouTube privacy is `private`; change `YOUTUBE_PRIVACY_STATUS` to `public` or `unlisted` if needed.
