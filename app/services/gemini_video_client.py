"""Gemini video generation client."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors

from app.config import settings

logger = logging.getLogger(__name__)

GENERATION_TIMEOUT_SECONDS = 600


class GeminiVideoError(Exception):
    pass


class GeminiVideoClient:
    def __init__(self) -> None:
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def generate_video(self, prompt: str, output_path: Path) -> Path:
        try:
            interaction = self._client.interactions.create(
                model=settings.gemini_video_model,
                input=prompt,
                generation_config={"video_config": {"task": "text_to_video"}},
                timeout=GENERATION_TIMEOUT_SECONDS,
            )
        except genai_errors.APIError as exc:
            raise GeminiVideoError(
                f"Gemini video generation failed for model "
                f"'{settings.gemini_video_model}': {exc}"
            ) from exc

        video = interaction.output_video
        if video is None or not video.data:
            raise GeminiVideoError(
                "Gemini completed video generation without returning video data."
            )

        try:
            video_bytes = base64.b64decode(video.data, validate=True)
        except (ValueError, TypeError) as exc:
            raise GeminiVideoError(
                "Gemini returned invalid base64 video data."
            ) from exc
        if not video_bytes:
            raise GeminiVideoError("Gemini returned an empty video.")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(video_bytes)
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise GeminiVideoError(f"Generated video was not written: {output_path}")

        logger.info("Generated Gemini video: %s", output_path)
        return output_path
