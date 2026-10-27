import json
import logging
import re
import time
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import settings
from app.prompts.midnight_souls import DEFAULT_NEGATIVE_PROMPT, VIDEO_PROMPT_SYSTEM
from app.schemas import ShortsMetadata, VideoClipPrompt, VideoPromptPlan
from app.utils.metadata_rules import normalize_metadata

logger = logging.getLogger(__name__)
GEMINI_FILE_POLL_INTERVAL_SECONDS = 2
GEMINI_FILE_POLL_TIMEOUT_SECONDS = 180
GEMINI_GENERATION_ATTEMPTS = 4  # Initial request plus three retries per model.

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


class GeminiMetadataError(Exception):
    pass


class GeminiMetadataClient:
    def __init__(self) -> None:
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def generate_video_prompts(
        self,
        target_duration_seconds: float | None = None,
    ) -> VideoPromptPlan:
        preferred = target_duration_seconds or settings.generate_shorts_target_duration_seconds
        max_duration = float(settings.generate_shorts_max_duration_seconds)
        duration = max(10.0, min(max_duration, float(preferred)))
        user_message = (
            f"Invent a new Midnight Souls Shorts scene. "
            f"Prefer target_duration_seconds around {duration:.0f}."
        )
        response = self._generate_content(
            contents=[VIDEO_PROMPT_SYSTEM, user_message],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.9,
            ),
        )

        raw_text = (response.text or "").strip()
        if not raw_text:
            raise GeminiMetadataError("Gemini returned an empty video-prompt response.")

        payload = _parse_json_response(raw_text)
        return _normalize_video_prompt_plan(
            payload,
            preferred_duration=duration,
            max_duration=max_duration,
        )

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
            response = self._generate_content(
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

    def _generate_content(
        self,
        *,
        contents: list[str | types.Part],
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse:
        models = [settings.gemini_model]
        fallback_model = settings.gemini_fallback_model.strip()
        if fallback_model and fallback_model not in models:
            models.append(fallback_model)

        # Keep SDK retries from multiplying the application retry budget.
        config = config.model_copy(update={
            "http_options": types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        })
        last_error = None
        for model in models:
            try:
                return Retrying(
                    retry=retry_if_exception_type(genai_errors.ServerError),
                    wait=wait_exponential_jitter(initial=1, max=4),
                    stop=stop_after_attempt(GEMINI_GENERATION_ATTEMPTS),
                    before_sleep=before_sleep_log(logger, logging.WARNING),
                    reraise=True,
                )(
                    self._client.models.generate_content,
                    model=model,
                    contents=contents,
                    config=config,
                )
            except genai_errors.ClientError as exc:
                raise self._map_client_error(exc, model=model) from exc
            except genai_errors.ServerError as exc:
                last_error = exc
                if model != models[-1]:
                    logger.warning(
                        "Gemini model '%s' failed after %d attempts; falling back to '%s'",
                        model,
                        GEMINI_GENERATION_ATTEMPTS,
                        fallback_model,
                    )

        raise GeminiMetadataError(
            f"Gemini is temporarily unavailable after retries for model(s): "
            f"{', '.join(models)}. Please try again later."
        ) from last_error

    def _map_client_error(
        self, exc: genai_errors.ClientError, *, model: str
    ) -> GeminiMetadataError:
        status_code = exc.code
        if status_code == 429:
            return GeminiMetadataError(
                f"Gemini quota exceeded for model '{model}'. "
                "Wait a minute and retry, check usage at https://ai.dev/rate-limit, "
                "or try a different model in GEMINI_MODEL (e.g. gemini-3.5-flash)."
            )
        return GeminiMetadataError(f"Gemini API error ({status_code}): {exc}")


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
    *,
    max_duration: float = 20.0,
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
        hint = item.get(
            "duration_hint_seconds",
            preferred_duration / max(len(clips_raw[:4]), 1),
        )
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
    target_f = max(10.0, min(max_duration, target_f))

    negative = str(payload.get("negative_prompt") or DEFAULT_NEGATIVE_PROMPT).strip()
    if not negative:
        negative = DEFAULT_NEGATIVE_PROMPT

    scene_summary = str(
        payload.get("scene_summary") or "Cozy ambient Midnight Souls scene"
    ).strip()

    music_phrase = str(payload.get("music_search_phrase") or "").strip()
    if not music_phrase:
        music_phrase = "cozy ambient rain"
    music_phrase = " ".join(music_phrase.split()[:4])

    return VideoPromptPlan(
        scene_summary=scene_summary,
        target_duration_seconds=target_f,
        music_search_phrase=music_phrase,
        negative_prompt=negative,
        clips=clips,
    )
