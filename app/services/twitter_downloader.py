import logging
from pathlib import Path

import yt_dlp

logger = logging.getLogger(__name__)


class TwitterDownloadError(Exception):
    pass


def download_twitter_video(url: str, output_dir: Path, job_id: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / f"{job_id}.%(ext)s")

    ydl_opts: dict = {
        "outtmpl": output_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        logger.exception("Failed to download Twitter video from %s", url)
        raise TwitterDownloadError(
            f"Could not download video from this post: {exc}"
        ) from exc

    if not info:
        raise TwitterDownloadError("No media information returned for this post.")

    downloaded_path = _resolve_downloaded_path(output_dir, job_id, info)
    if not downloaded_path.exists():
        raise TwitterDownloadError("Video download completed but file was not found.")

    return downloaded_path


def _resolve_downloaded_path(output_dir: Path, job_id: str, info: dict) -> Path:
    requested = info.get("requested_downloads") or []
    if requested:
        filepath = requested[0].get("filepath")
        if filepath:
            return Path(filepath)

    ext = info.get("ext") or "mp4"
    candidate = output_dir / f"{job_id}.{ext}"
    if candidate.exists():
        return candidate

    for path in output_dir.glob(f"{job_id}.*"):
        if path.is_file():
            return path

    raise TwitterDownloadError("Downloaded video file could not be resolved.")
