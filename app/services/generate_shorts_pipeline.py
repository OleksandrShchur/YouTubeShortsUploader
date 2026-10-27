"""Generate Midnight Souls Shorts via Gemini video generation + Pixabay music."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from app.config import settings
from app.schemas import VideoPromptPlan
from app.services.ffmpeg_utils import (
    FFmpegError,
    concat_videos,
    mux_audio_onto_video,
    normalize_to_shorts,
    probe_duration_seconds,
)
from app.services.gemini_video_client import GeminiVideoClient, GeminiVideoError
from app.services.pixabay_audio_client import (
    PixabayAudioError,
    PixabayAudioResult,
    find_and_download_audio,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

MIN_DURATION_SECONDS = 10.0
MAX_DURATION_SECONDS = 20.0
MIN_CLIP_SECONDS = 4.0
MAX_CLIP_SECONDS = 10.0


class GenerateShortsPipelineError(Exception):
    pass


class GenerateShortsPipeline:
    def __init__(self) -> None:
        self._video_client = GeminiVideoClient()

    def render_from_plan(
        self,
        plan: VideoPromptPlan,
        storage_dir: Path,
        job_id: str,
        *,
        used_audio_ids: list[int] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[Path, PixabayAudioResult, Path]:
        """Render Gemini video clips, mux Pixabay music.

        Returns (muxed_path, audio_result, silent_path).
        """

        def progress(message: str) -> None:
            logger.info("[%s] %s", job_id, message)
            if on_progress:
                on_progress(message)

        silent_path = storage_dir / f"{job_id}_silent.mp4"
        audio_path = storage_dir / f"{job_id}_audio.mp3"
        muxed_path = storage_dir / f"{job_id}.mp4"
        clip_dir = storage_dir / f"{job_id}_clips"

        try:
            clip_dir.mkdir(parents=True, exist_ok=True)
            rendered_clips: list[Path] = []
            for clip_number, clip in enumerate(plan.clips, start=1):
                duration = _clamp_clip_duration(clip.duration_hint_seconds)
                progress(
                    f"Generating scene {clip_number}/{len(plan.clips)} with Gemini..."
                )
                generated_path = clip_dir / f"{clip_number}_generated.mp4"
                normalized_path = clip_dir / f"{clip_number}.mp4"
                self._video_client.generate_video(
                    _build_video_prompt(plan, clip_number, clip.prompt, duration),
                    generated_path,
                )
                normalize_to_shorts(
                    generated_path,
                    normalized_path,
                    max_duration=duration,
                )
                rendered_clips.append(normalized_path)

            progress("Combining generated scenes...")
            concat_videos(rendered_clips, silent_path)

            duration = probe_duration_seconds(silent_path)
            max_allowed = min(
                MAX_DURATION_SECONDS,
                float(settings.generate_shorts_max_duration_seconds),
            )
            if duration < MIN_DURATION_SECONDS:
                raise GenerateShortsPipelineError(
                    f"Rendered video is only {duration:.1f}s "
                    f"(need {MIN_DURATION_SECONDS:.0f}-{max_allowed:.0f}s)."
                )
            if duration > max_allowed + 0.5:
                raise GenerateShortsPipelineError(
                    f"Rendered video is {duration:.1f}s "
                    f"(max {max_allowed:.0f}s). Regenerate with shorter clips."
                )

            phrase = plan.music_search_phrase.strip()
            progress(f"Searching Pixabay Music:\n{phrase}")
            audio_result = find_and_download_audio(
                phrase,
                storage_dir,
                job_id,
                min_duration_seconds=duration,
                used_ids=used_audio_ids,
                filename=audio_path.name,
            )
            progress("Muxing Pixabay music onto video...")
            mux_audio_onto_video(silent_path, audio_result.local_path, muxed_path)
            progress(f"Ready ({duration:.1f}s).")
            return muxed_path, audio_result, silent_path
        except (GeminiVideoError, PixabayAudioError, FFmpegError) as exc:
            raise GenerateShortsPipelineError(str(exc)) from exc


def _build_video_prompt(
    plan: VideoPromptPlan,
    clip_number: int,
    clip_prompt: str,
    duration: float,
) -> str:
    prompt = (
        f"Create one cinematic vertical 9:16 video clip, approximately "
        f"{duration:.0f} seconds long, for a YouTube Short. "
        f"The overall scene is: {plan.scene_summary.strip()}. "
        "Keep the composition, subject, lighting, colors, weather, and camera "
        "consistent with the other clips in this same scene. "
        "Use slow, calm ambient motion and a cinematic 4K aesthetic. "
        "Do not include speech, music, text, captions, logos, or watermarks. "
        f"Scene {clip_number} prompt: {clip_prompt.strip()}"
    )
    if plan.negative_prompt:
        prompt += f" Avoid: {plan.negative_prompt.strip()}."
    return prompt


def _clamp_clip_duration(hint: float) -> float:
    try:
        value = float(hint)
    except (TypeError, ValueError):
        value = 6.0
    return max(MIN_CLIP_SECONDS, min(MAX_CLIP_SECONDS, value))
