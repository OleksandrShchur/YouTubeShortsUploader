# YouTube Shorts Uploader

Telegram bot pipeline for publishing YouTube Shorts. Three flows:

1. **`/twitter`** — download a video from an X/Twitter post, generate metadata with Gemini, review, publish.
2. **`/pixabay`** — pick 3 random tags, download a vertical HD Pixabay video, scrape matching Pixabay Music, mux audio trimmed to the video length, review video then metadata, publish.
3. **`/pixabay_url`** — paste a Pixabay video page URL, take its first tags for music search, mux audio, review video then metadata, publish.

## Flows

### Twitter

1. Admin sends `/twitter`, then an X/Twitter post URL.
2. Server clears `storage/videos/` and downloads the video there.
3. Server sends the video to Gemini and receives JSON metadata (`title`, `description`, `viral_title_tags`, `shorts_tags`).
4. Bot returns formatted JSON with **Approve**, **Decline**, **Modify**.
5. **Approve** uploads to YouTube Shorts, then deletes the local video.
6. **Decline** deletes the local video and stops.
7. **Modify** asks for edited JSON; bot re-shows the review keyboard.

### Pixabay (Midnight Souls stock)

1. Admin sends `/pixabay` → confirmation with **Start** / **Back to menu** (safe for misclicks).
2. **Start** → bot picks **exactly 3** random tags from the predefined library as the search query.
3. Pixabay Video API search; bot picks an unused **vertical HD** film clip with duration **1–60s**.
4. Downloads the highest-resolution vertical stream **as-is** (no re-encode) to a silent sidecar file.
5. Unofficial Pixabay Music scrape with the **same 3 tags**; downloads an MP3 whose duration is at least the video length.
6. ffmpeg muxes audio onto the video, trimming audio to **exactly** the video duration.
7. Bot sends the muxed Short with video + music attribution and four buttons:
   - **Approve** → Gemini metadata JSON + second review (same as Twitter/HF).
   - **Decline** → delete and stop.
   - **Modify audio** → keep the silent video + same tags; fetch a different unused track and remux.
   - **Modify video** → new 3-tag set → new video + new music.
8. **Approve** (metadata) → YouTube upload on the same OAuth channel.

### Pixabay URL

1. Admin sends `/pixabay_url`, then a Pixabay video page URL (e.g. `https://pixabay.com/videos/example-123456/`).
2. Bot resolves the video via the Pixabay Videos API `id` lookup and downloads a **vertical 9:16 HD/4K** stream (duration **1–60s**). Non-matching videos are rejected.
3. Takes the **first up to 3 tags** from the video (fewer is fine; zero tags is rejected).
4. Unofficial Pixabay Music scrape with those tags; muxes audio trimmed to the video length (same as `/pixabay`).
5. Bot sends the muxed Short with three buttons:
   - **Approve** → Gemini metadata JSON + second review (same as other flows).
   - **Change audio** → keep the silent video + same tags; fetch a different unused track and remux.
   - **Decline** → delete and return to the main menu.
6. **Approve** (metadata) → YouTube upload on the same OAuth channel.

> Music is **not** covered by the official Pixabay API. The bot scrapes Pixabay Music HTML/CDN links. This can break if Pixabay or Cloudflare changes.

## Requirements

- Python 3.12+
- [ffmpeg](https://ffmpeg.org/) (yt-dlp merge + Pixabay audio mux)
- Telegram bot token
- Gemini API key (Google AI Studio)
- Pixabay API key (`PIXABAY_API_KEY`) — for `/pixabay` and `/pixabay_url`
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
4. For production, set `TELEGRAM_WEBHOOK_URL` to your public HTTPS base (Telegram pushes updates there). Leave it empty locally to fall back to long polling.

### 4. Gemini API

1. Create an API key in [Google AI Studio](https://aistudio.google.com/).
2. Set `GEMINI_API_KEY`.
3. Optionally set `GEMINI_MODEL` (default: `gemini-3.5-flash`).

### 5. Pixabay API

1. Open [Pixabay API docs](https://pixabay.com/api/docs/).
2. Sign up or log in to Pixabay.
3. On that page, your personal API key appears in the `key` parameter section.
4. Set `PIXABAY_API_KEY` in `.env`.
5. Restart the bot. `/pixabay` and `/pixabay_url` refuse to start if the key is missing.

### 6. YouTube OAuth

1. In [Google Cloud Console](https://console.cloud.google.com/), enable **YouTube Data API v3**.
2. Create OAuth credentials for a **Desktop app**.
3. Download the JSON file and save it as `secrets/client_secret.json`.
4. On first upload, the app opens a browser for one-time OAuth. The refresh token is saved to `secrets/youtube_token.json`.

For Docker or headless deployment, generate `secrets/youtube_token.json` locally first, then provide it to the container (see [Docker](#docker)).

### 7. Run locally

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Without `TELEGRAM_WEBHOOK_URL`, the bot uses long polling. For webhook mode locally, expose HTTPS (e.g. ngrok / Cloudflare Tunnel) and set:

```bash
TELEGRAM_WEBHOOK_URL=https://your-tunnel.example
TELEGRAM_WEBHOOK_PATH=/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=a-long-random-string
```

Health check: `GET http://localhost:8000/health`

## Docker

Build and run with a `.env` file:

```bash
docker build -t youtube-shorts-uploader .
docker run --env-file .env -p 7860:7860 youtube-shorts-uploader
```

Health check: `GET http://localhost:7860/health`

The container listens on port **7860** and includes ffmpeg. Set `TELEGRAM_WEBHOOK_URL` to the container’s public HTTPS base (for example `https://your-space.hf.space`) so Telegram uses webhooks instead of long polling.

OAuth credential files can be supplied in two ways:

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
| `TELEGRAM_WEBHOOK_URL` | production | — | Public HTTPS base URL; enables webhooks when set |
| `TELEGRAM_WEBHOOK_PATH` | no | `/telegram/webhook` | Path Telegram POSTs updates to |
| `TELEGRAM_WEBHOOK_SECRET` | no | — | Shared secret verified via `X-Telegram-Bot-Api-Secret-Token` |
| `GEMINI_API_KEY` | yes | — | Google AI Studio API key |
| `GEMINI_MODEL` | no | `gemini-3.5-flash` | Gemini model for prompts/metadata |
| `PIXABAY_API_KEY` | for `/pixabay` and `/pixabay_url` | — | Pixabay API key from [api docs](https://pixabay.com/api/docs/) |
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
2. Use `/twitter` with an X/Twitter URL, `/pixabay` for a stock Short, or `/pixabay_url` with a Pixabay video page URL.
3. Review video (Pixabay) and/or metadata JSON, then choose an action.

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
  main.py              # FastAPI app + Telegram webhook/polling lifecycle
  bot.py               # Telegram handlers and review flows
  config.py            # Settings from environment
  schemas.py           # Pydantic models
  session_store.py     # In-memory job state
  data/
    pixabay_tags.py    # Lazy loader + append helper for Pixabay tags
    pixabay_tags.txt   # One search tag per line (appendable)
  services/
    twitter_downloader.py
    pixabay_client.py
    pixabay_audio_client.py  # Unofficial Music HTML/CDN scrape
    gemini_client.py
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

- Prefer webhooks in production (`TELEGRAM_WEBHOOK_URL`); long polling is only for local use when that variable is unset.
- Only the configured `ADMIN_CHAT_ID` can use the bot.
- Session state is in-memory only; restarting the server clears pending jobs.
- Stale pending sessions and videos older than `SESSION_TTL_HOURS` are cleaned on startup.
- Starting a new Twitter or Pixabay job clears leftover files in `storage/videos/`.
- Pixabay videos are downloaded without re-encoding to preserve quality; only already-vertical HD clips are used. Music is scraped unofficially and muxed with ffmpeg (audio trimmed to video length).
- Default YouTube privacy is `private`; change `YOUTUBE_PRIVACY_STATUS` to `public` or `unlisted` if needed.
