import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

SHORTS_WIDTH = 1080
SHORTS_HEIGHT = 1920
MIN_DURATION_SECONDS = 8.0
MAX_DURATION_SECONDS = 15.0


class FFmpegError(Exception):
    pass


def _require_binaries() -> None:
    if shutil.which("ffmpeg") is None:
        raise FFmpegError("ffmpeg is not installed or not on PATH.")
    if shutil.which("ffprobe") is None:
        raise FFmpegError("ffprobe is not installed or not on PATH.")


def _run(cmd: list[str], *, error_prefix: str) -> subprocess.CompletedProcess[str]:
    logger.debug("Running: %s", " ".join(cmd))
    try:
        return subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise FFmpegError(f"{error_prefix}: {detail}") from exc


def probe_duration_seconds(video_path: Path) -> float:
    _require_binaries()
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        error_prefix="Failed to probe video duration",
    )
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise FFmpegError(f"Could not parse duration for {video_path}") from exc


def extract_last_frame(video_path: Path, output_image: Path) -> Path:
    _require_binaries()
    output_image.parent.mkdir(parents=True, exist_ok=True)
    duration = probe_duration_seconds(video_path)
    seek_at = max(0.0, duration - 0.05)
    _run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{seek_at:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output_image),
        ],
        error_prefix="Failed to extract last frame",
    )
    if not output_image.exists():
        raise FFmpegError(f"Last frame was not written: {output_image}")
    return output_image


def normalize_to_shorts(
    input_path: Path,
    output_path: Path,
    *,
    max_duration: float = MAX_DURATION_SECONDS,
) -> Path:
    """Scale/pad to 1080x1920 vertical HD and optionally trim to max_duration."""
    _require_binaries()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale={SHORTS_WIDTH}:{SHORTS_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={SHORTS_WIDTH}:{SHORTS_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,fps=30"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-t",
        f"{max_duration:.3f}",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-an",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    _run(cmd, error_prefix="Failed to normalize video to Shorts format")
    if not output_path.exists():
        raise FFmpegError(f"Normalized video was not written: {output_path}")
    return output_path


def concat_videos(clip_paths: list[Path], output_path: Path) -> Path:
    if not clip_paths:
        raise FFmpegError("No clips provided for concatenation.")
    if len(clip_paths) == 1:
        shutil.copyfile(clip_paths[0], output_path)
        return output_path

    _require_binaries()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = output_path.with_suffix(".txt")
    try:
        lines = []
        for path in clip_paths:
            escaped = str(path.resolve()).replace("'", r"'\''")
            lines.append(f"file '{escaped}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c",
                "copy",
                str(output_path),
            ],
            error_prefix="Failed to concatenate video clips",
        )
    finally:
        if list_file.exists():
            list_file.unlink(missing_ok=True)

    if not output_path.exists():
        raise FFmpegError(f"Concatenated video was not written: {output_path}")
    return output_path


def ensure_duration_window(
    video_path: Path,
    *,
    min_seconds: float = MIN_DURATION_SECONDS,
    max_seconds: float = MAX_DURATION_SECONDS,
) -> float:
    duration = probe_duration_seconds(video_path)
    if duration < min_seconds:
        raise FFmpegError(
            f"Final video is too short ({duration:.2f}s). Need at least {min_seconds:.0f}s."
        )
    if duration > max_seconds + 0.25:
        raise FFmpegError(
            f"Final video is too long ({duration:.2f}s). Max is {max_seconds:.0f}s."
        )
    return duration
