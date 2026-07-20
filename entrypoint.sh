#!/bin/sh
set -e

mkdir -p secrets storage/videos

if [ -n "$YOUTUBE_CLIENT_SECRETS_JSON" ]; then
  printf '%s' "$YOUTUBE_CLIENT_SECRETS_JSON" > secrets/client_secret.json
fi

if [ -n "$YOUTUBE_TOKEN_JSON" ]; then
  printf '%s' "$YOUTUBE_TOKEN_JSON" > secrets/youtube_token.json
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 7860
