import json
import logging
import re
from pathlib import Path

from google import genai
from google.genai import types

from app.config import settings
from app.schemas import ShortsMetadata
from app.utils.metadata_rules import normalize_metadata

logger = logging.getLogger(__name__)

METADATA_PROMPT = """You are a YouTube Shorts metadata expert.

Analyze this video and return ONLY valid JSON (no markdown, no code fences) with this exact structure:
{
  "title": "catchy title without hashtags",
  "description": "engaging description for YouTube Shorts, can include line breaks",
  "viral_title_tags": ["tag1", "tag2", "tag3"],
  "shorts_tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]
}

Rules:
- title must be at most 70 characters (before hashtags) so that title + 3-4 viral hashtags fit within 100 characters total
- viral_title_tags must contain exactly 3 or 4 short viral hashtags (without # prefix)
- shorts_tags must contain all relevant tags for the YouTube tags field (5-15 tags, no # prefix)
- description should be optimized for Shorts discovery
- return JSON only
"""


class GeminiMetadataError(Exception):
    pass


class GeminiMetadataClient:
    def __init__(self) -> None:
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def generate_metadata(self, video_path: Path) -> ShortsMetadata:
        if not video_path.exists():
            raise GeminiMetadataError(f"Video file not found: {video_path}")

        uploaded_file = self._client.files.upload(file=str(video_path))

        try:
            response = self._client.models.generate_content(
                model=settings.gemini_model,
                contents=[
                    types.Part.from_uri(
                        file_uri=uploaded_file.uri,
                        mime_type=uploaded_file.mime_type or "video/mp4",
                    ),
                    METADATA_PROMPT,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.7,
                ),
            )
        finally:
            try:
                if uploaded_file.name:
                    self._client.files.delete(name=uploaded_file.name)
            except Exception:
                logger.warning("Failed to delete uploaded Gemini file", exc_info=True)

        raw_text = (response.text or "").strip()
        if not raw_text:
            raise GeminiMetadataError("Gemini returned an empty response.")

        payload = _parse_json_response(raw_text)
        return normalize_metadata(payload)


def _parse_json_response(text: str) -> dict:
    cleaned = text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GeminiMetadataError("Gemini response was not valid JSON.") from exc

    if not isinstance(data, dict):
        raise GeminiMetadataError("Gemini JSON root must be an object.")
    return data
