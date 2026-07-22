import logging
import random
from collections.abc import Callable
from pathlib import Path

from app.config import settings
from app.schemas import VideoPromptPlan
from app.services.ffmpeg_utils import (
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
    FFmpegError,
    concat_videos,
    ensure_duration_window,
    extract_last_frame,
    normalize_to_shorts,
    probe_duration_seconds,
)
from app.services.gemini_client import GeminiMetadataClient, GeminiMetadataError
from app.services.huggingface_video import HuggingFaceVideoClient, HuggingFaceVideoError

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]


class VideoPipelineError(Exception):
    pass


class HuggingFaceVideoPipeline:
    def __init__(
        self,
        gemini_client: GeminiMetadataClient | None = None,
        hf_client: HuggingFaceVideoClient | None = None,
    ) -> None:
        self._gemini = gemini_client or GeminiMetadataClient()
        self._hf = hf_client

    def _get_hf(self) -> HuggingFaceVideoClient:
        if self._hf is None:
            self._hf = HuggingFaceVideoClient()
        return self._hf

    def generate(
        self,
        storage_dir: Path,
        job_id: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[Path, VideoPromptPlan]:
        def progress(message: str) -> None:
            logger.info("[%s] %s", job_id, message)
            if on_progress:
                on_progress(message)

        progress("Inventing scene with Gemini...")
        try:
            plan = self._gemini.generate_video_prompts(
                target_duration_seconds=settings.hf_target_duration_seconds,
            )
        except GeminiMetadataError as exc:
            raise VideoPipelineError(str(exc)) from exc

        work_dir = storage_dir / f"{job_id}_clips"
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            clip_paths = self._generate_clips(plan, work_dir, job_id, progress)
            progress("Merging and normalizing to 1080x1920...")
            final_path = self._merge_and_normalize(clip_paths, storage_dir, job_id)
            duration = ensure_duration_window(final_path)
            progress(f"Ready ({duration:.1f}s).")
            return final_path, plan
        except (HuggingFaceVideoError, FFmpegError) as exc:
            raise VideoPipelineError(str(exc)) from exc
        finally:
            self._cleanup_dir(work_dir)

    def _generate_clips(
        self,
        plan: VideoPromptPlan,
        work_dir: Path,
        job_id: str,
        progress: ProgressCallback,
    ) -> list[Path]:
        hf = self._get_hf()
        seed = random.randint(1, 2_000_000_000)
        clip_paths: list[Path] = []
        total = len(plan.clips)

        for i, clip in enumerate(plan.clips):
            progress(f"Generating clip {i + 1}/{total}...")
            raw_path = work_dir / f"raw_{i + 1}.mp4"
            num_frames = _frames_for_hint(clip.duration_hint_seconds)

            if i == 0:
                hf.text_to_video(
                    clip.prompt,
                    raw_path,
                    negative_prompt=plan.negative_prompt,
                    num_frames=num_frames,
                    seed=seed,
                )
            else:
                prev = clip_paths[-1]
                frame_path = work_dir / f"frame_{i}.jpg"
                try:
                    extract_last_frame(prev, frame_path)
                    hf.image_to_video(
                        frame_path,
                        clip.prompt,
                        raw_path,
                        negative_prompt=plan.negative_prompt,
                        num_frames=num_frames,
                        seed=seed + i,
                    )
                except (HuggingFaceVideoError, FFmpegError) as exc:
                    logger.warning(
                        "I2V continuity failed for clip %s (%s); falling back to T2V",
                        i + 1,
                        exc,
                    )
                    continuation = (
                        f"Seamless continuation of the exact same scene, lighting, palette, "
                        f"and camera: {clip.prompt}"
                    )
                    hf.text_to_video(
                        continuation,
                        raw_path,
                        negative_prompt=plan.negative_prompt,
                        num_frames=num_frames,
                        seed=seed + i,
                    )

            normalized = work_dir / f"norm_{i + 1}.mp4"
            normalize_to_shorts(raw_path, normalized, max_duration=MAX_DURATION_SECONDS)
            clip_paths.append(normalized)

            # If the first clip alone already meets the window, stop early.
            if i == 0:
                duration = probe_duration_seconds(normalized)
                if MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS:
                    progress(
                        f"Single clip is {duration:.1f}s; skipping additional clips."
                    )
                    return [normalized]

        return clip_paths

    def _merge_and_normalize(
        self,
        clip_paths: list[Path],
        storage_dir: Path,
        job_id: str,
    ) -> Path:
        if not clip_paths:
            raise VideoPipelineError("No clips were generated.")

        concat_path = storage_dir / f"{job_id}_concat.mp4"
        final_path = storage_dir / f"{job_id}.mp4"

        if len(clip_paths) == 1:
            normalize_to_shorts(clip_paths[0], final_path, max_duration=MAX_DURATION_SECONDS)
            return final_path

        concat_videos(clip_paths, concat_path)
        try:
            normalize_to_shorts(concat_path, final_path, max_duration=MAX_DURATION_SECONDS)
        finally:
            if concat_path.exists():
                concat_path.unlink(missing_ok=True)

        # Prefer adding more clips if still short — raise so bot can show error.
        duration = probe_duration_seconds(final_path)
        if duration < MIN_DURATION_SECONDS:
            raise VideoPipelineError(
                f"Merged video is only {duration:.1f}s (need {MIN_DURATION_SECONDS:.0f}-"
                f"{MAX_DURATION_SECONDS:.0f}s). Try Modify to regenerate."
            )
        return final_path

    @staticmethod
    def _cleanup_dir(path: Path) -> None:
        if not path.exists():
            return
        for child in path.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
        try:
            path.rmdir()
        except OSError:
            logger.debug("Could not remove work dir %s", path, exc_info=True)


def _frames_for_hint(duration_hint_seconds: float, fps: int = 24) -> int:
    frames = int(round(max(1.0, duration_hint_seconds) * fps))
    # Common video models prefer odd frame counts near 4k+1.
    if frames % 4 != 1:
        frames = max(9, (frames // 4) * 4 + 1)
    return min(frames, 241)
