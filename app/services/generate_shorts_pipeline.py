"""Generate Midnight Souls Shorts via Gemini prompts + JSON2Video + Pixabay music."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from app.config import settings
from app.schemas import VideoPromptPlan
from app.services.ffmpeg_utils import (
    FFmpegError,
    mux_audio_onto_video,
    probe_duration_seconds,
)
from app.services.json2video_client import (
    Json2VideoError,
    create_movie,
    download_movie,
    wait_for_movie,
)
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
    def render_from_plan(
        self,
        plan: VideoPromptPlan,
        storage_dir: Path,
        job_id: str,
        *,
        used_audio_ids: list[int] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[Path, PixabayAudioResult, Path]:
        """Render silent video via JSON2Video, mux Pixabay music.

        Returns (muxed_path, audio_result, silent_path).
        """

        def progress(message: str) -> None:
            logger.info("[%s] %s", job_id, message)
            if on_progress:
                on_progress(message)

        silent_path = storage_dir / f"{job_id}_silent.mp4"
        audio_path = storage_dir / f"{job_id}_audio.mp3"
        muxed_path = storage_dir / f"{job_id}.mp4"

        try:
            progress("Submitting scenes to JSON2Video...")
            payload = build_movie_payload(plan, cache=False)
            project_id = create_movie(payload)
            progress(f"Rendering JSON2Video project {project_id}...")
            movie_url = wait_for_movie(project_id)
            progress("Downloading rendered video...")
            download_movie(movie_url, silent_path)

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
        except (Json2VideoError, PixabayAudioError, FFmpegError) as exc:
            raise GenerateShortsPipelineError(str(exc)) from exc


def build_movie_payload(plan: VideoPromptPlan, *, cache: bool = False) -> dict:
    """Build a vertical Shorts movie with one AI video element per clip scene."""
    model = settings.json2video_video_model
    scenes: list[dict] = []
    for clip in plan.clips:
        duration = _clamp_clip_duration(clip.duration_hint_seconds)
        prompt = clip.prompt.strip()
        if plan.negative_prompt:
            prompt = f"{prompt}. Avoid: {plan.negative_prompt}"
        scenes.append(
            {
                "duration": duration,
                "elements": [
                    {
                        "type": "video",
                        "model": model,
                        "prompt": prompt,
                        "duration": duration,
                        "aspect-ratio": "vertical",
                        "resize": "cover",
                        "generate_audio": False,
                        "resolution": "720p",
                        "cache": cache,
                    }
                ],
            }
        )

    return {
        "resolution": "instagram-story",
        "quality": "high",
        "cache": cache,
        "comment": f"Midnight Souls Short: {plan.scene_summary}",
        "scenes": scenes,
    }


def _clamp_clip_duration(hint: float) -> float:
    try:
        value = float(hint)
    except (TypeError, ValueError):
        value = 6.0
    return max(MIN_CLIP_SECONDS, min(MAX_CLIP_SECONDS, value))
