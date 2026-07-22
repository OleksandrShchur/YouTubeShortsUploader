import json
import logging
import re
import time
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.config import settings
from app.prompts.midnight_souls import BRAND_BRIEF, CHANNEL_NAME, DEFAULT_NEGATIVE_PROMPT, VIDEO_PROMPT_SYSTEM
from app.schemas import ShortsMetadata, VideoClipPrompt, VideoPromptPlan
from app.utils.metadata_rules import normalize_metadata

logger = logging.getLogger(__name__)
GEMINI_FILE_POLL_INTERVAL_SECONDS = 2
GEMINI_FILE_POLL_TIMEOUT_SECONDS = 180

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
- lean into cozy ambient / lofi / nature / study-relaxation vibes when they fit the video
- return JSON only
"""

PIXABAY_PHRASE_PROMPT = f"""You invent a single stock-video search phrase for the YouTube channel {CHANNEL_NAME}.

{BRAND_BRIEF}

Return ONLY the search phrase as plain text (no quotes, no markdown, no JSON).

Rules:
- One short English phrase suitable for Pixabay video search (at most 4 words). Start with current season
- Example style: summer rainy window.
- Prefer cozy ambient / nature / rain / fireplace / misty forest / study nook / soft interior scenes.
- Avoid celebrity names, brands, logos, text overlays, faces looking at camera, neon cyberpunk.
- Do not include hashtags or punctuation fluff.
- Invent a fresh phrase each time.
"""


class GeminiMetadataError(Exception):
    pass


class GeminiMetadataClient:
    def __init__(self) -> None:
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def generate_pixabay_phrase(self) -> str:
        try:
            response = self._client.models.generate_content(
                model=settings.gemini_model,
                contents=[
                    PIXABAY_PHRASE_PROMPT,
                    "Invent today's Midnight Souls Pixabay search phrase.",
                ],
                config=types.GenerateContentConfig(temperature=0.95),
            )
        except genai_errors.ClientError as exc:
            raise self._map_client_error(exc) from exc

        raw_text = (response.text or "").strip()
        phrase = _normalize_pixabay_phrase(raw_text)
        if not phrase:
            raise GeminiMetadataError("Gemini returned an empty Pixabay search phrase.")
        return phrase

    def generate_video_prompts(
        self,
        target_duration_seconds: float | None = None,
    ) -> VideoPromptPlan:
        duration = target_duration_seconds or settings.hf_target_duration_seconds
        duration = max(8.0, min(15.0, float(duration)))
        user_message = (
            f"Invent a new Midnight Souls Shorts scene. "
            f"Prefer target_duration_seconds around {duration:.0f}."
        )
        try:
            response = self._client.models.generate_content(
                model=settings.gemini_model,
                contents=[VIDEO_PROMPT_SYSTEM, user_message],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.9,
                ),
            )
        except genai_errors.ClientError as exc:
            raise self._map_client_error(exc) from exc

        raw_text = (response.text or "").strip()
        if not raw_text:
            raise GeminiMetadataError("Gemini returned an empty video-prompt response.")

        payload = _parse_json_response(raw_text)
        return _normalize_video_prompt_plan(payload, preferred_duration=duration)

    def generate_metadata(self, video_path: Path) -> ShortsMetadata:
        if not video_path.exists():
            raise GeminiMetadataError(f"Video file not found: {video_path}")

        uploaded_file = self._client.files.upload(file=str(video_path))

        poll_started_at = time.monotonic()
        state = getattr(uploaded_file, "state", None)
        while state:
            state_name = getattr(state, "name", str(state)).upper()
            if state_name in {"ACTIVE", "STATE_UNSPECIFIED"}:
                break
            if time.monotonic() - poll_started_at >= GEMINI_FILE_POLL_TIMEOUT_SECONDS:
                raise GeminiMetadataError(
                    "Timed out waiting for Gemini to finish processing the uploaded video."
                )

            time.sleep(GEMINI_FILE_POLL_INTERVAL_SECONDS)
            uploaded_file = self._client.files.get(name=uploaded_file.name)
            state = getattr(uploaded_file, "state", None)

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
        except genai_errors.ClientError as exc:
            raise self._map_client_error(exc) from exc
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

    def _map_client_error(self, exc: genai_errors.ClientError) -> GeminiMetadataError:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            return GeminiMetadataError(
                f"Gemini quota exceeded for model '{settings.gemini_model}'. "
                "Wait a minute and retry, check usage at https://ai.dev/rate-limit, "
                "or try a different model in GEMINI_MODEL (e.g. gemini-3.5-flash)."
            )
        return GeminiMetadataError(f"Gemini API error ({status_code}): {exc}")


def _normalize_pixabay_phrase(text: str) -> str:
    cleaned = text.strip()
    fence_match = re.search(r"```(?:\w+)?\s*([\s\S]*?)\s*```", cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    # Prefer a JSON {"phrase": "..."} if the model ignored plain-text instructions.
    if cleaned.startswith("{"):
        try:
            payload = json.loads(cleaned)
            if isinstance(payload, dict):
                for key in ("phrase", "q", "query", "search"):
                    value = payload.get(key)
                    if value:
                        cleaned = str(value).strip()
                        break
        except json.JSONDecodeError:
            pass

    cleaned = cleaned.strip().strip('"').strip("'")
    cleaned = re.sub(r"\s+", " ", cleaned)
    words = cleaned.split()
    if len(words) > 4:
        cleaned = " ".join(words[:4])
    return cleaned


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


def _normalize_video_prompt_plan(
    payload: dict,
    preferred_duration: float,
) -> VideoPromptPlan:
    clips_raw = payload.get("clips")
    if not isinstance(clips_raw, list) or not clips_raw:
        raise GeminiMetadataError("Gemini video prompts must include a non-empty clips list.")

    clips: list[VideoClipPrompt] = []
    for idx, item in enumerate(clips_raw[:4], start=1):
        if not isinstance(item, dict):
            raise GeminiMetadataError("Each clip prompt must be an object.")
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            raise GeminiMetadataError(f"Clip {idx} is missing a prompt.")
        hint = item.get("duration_hint_seconds", preferred_duration / max(len(clips_raw[:4]), 1))
        try:
            hint_f = float(hint)
        except (TypeError, ValueError) as exc:
            raise GeminiMetadataError(f"Clip {idx} has invalid duration_hint_seconds.") from exc
        clips.append(
            VideoClipPrompt(
                index=int(item.get("index") or idx),
                duration_hint_seconds=max(1.0, min(15.0, hint_f)),
                prompt=prompt,
            )
        )

    if len(clips) < 2:
        # Ensure at least 2 clips for merge strategy when free-tier clips are short.
        first = clips[0]
        clips.append(
            VideoClipPrompt(
                index=2,
                duration_hint_seconds=first.duration_hint_seconds,
                prompt=(
                    f"Seamless continuation of the exact same scene and camera: {first.prompt}. "
                    "Same lighting, palette, and subject; gentle ongoing ambient motion only."
                ),
            )
        )

    target = payload.get("target_duration_seconds", preferred_duration)
    try:
        target_f = float(target)
    except (TypeError, ValueError):
        target_f = preferred_duration
    target_f = max(8.0, min(15.0, target_f))

    negative = str(payload.get("negative_prompt") or DEFAULT_NEGATIVE_PROMPT).strip()
    if not negative:
        negative = DEFAULT_NEGATIVE_PROMPT

    scene_summary = str(payload.get("scene_summary") or "Cozy ambient Midnight Souls scene").strip()

    return VideoPromptPlan(
        scene_summary=scene_summary,
        target_duration_seconds=target_f,
        negative_prompt=negative,
        clips=clips,
    )
