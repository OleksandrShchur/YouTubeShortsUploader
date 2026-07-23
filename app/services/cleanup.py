import logging
import shutil
from pathlib import Path

from app.config import settings
from app.schemas import JobSource
from app.session_store import session_store

logger = logging.getLogger(__name__)


def delete_video_file(video_path: str | Path) -> None:
    path = Path(video_path)
    if path.exists() and path.is_file():
        path.unlink()
        logger.info("Deleted video file: %s", path)


def delete_path(path: str | Path) -> None:
    """Delete a file or directory tree if it exists."""
    target = Path(path)
    if not target.exists():
        return
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
        logger.info("Deleted directory: %s", target)
        return
    if target.is_file():
        target.unlink()
        logger.info("Deleted video file: %s", target)


def delete_job_storage_files(job_id: str, storage_dir: Path | None = None) -> None:
    """Delete all known on-disk artifacts for a job id (any source)."""
    base_dir = storage_dir or settings.video_storage_path
    paths = [
        base_dir / f"{job_id}.mp4",
        base_dir / f"{job_id}_silent.mp4",
        base_dir / f"{job_id}_audio.mp3",
        base_dir / f"{job_id}_audio_tmp.mp3",
        base_dir / f"{job_id}_mux_tmp.mp4",
        base_dir / f"{job_id}_concat.mp4",
        base_dir / f"{job_id}.pending",
        base_dir / f"{job_id}_clips",
    ]
    for path in paths:
        delete_path(path)


def delete_pixabay_job_files(job_id: str, storage_dir: Path | None = None) -> None:
    """Delete muxed video plus silent/audio sidecars for a Pixabay job."""
    base_dir = storage_dir or settings.video_storage_path
    session = session_store.get(job_id)
    paths: list[Path] = []
    if session and session.video_path:
        paths.append(Path(session.video_path))
    if session and isinstance(session.pixabay_meta, dict):
        silent = session.pixabay_meta.get("silent_path")
        audio = session.pixabay_meta.get("audio_path")
        if silent:
            paths.append(Path(str(silent)))
        if audio:
            paths.append(Path(str(audio)))

    for path in paths:
        delete_path(path)
    delete_job_storage_files(job_id, base_dir)


def clear_video_storage_dir(storage_dir: Path) -> int:
    """Delete all files/dirs in video storage (keeps .gitkeep). Returns count deleted."""
    storage_dir.mkdir(parents=True, exist_ok=True)
    deleted = 0
    for path in list(storage_dir.iterdir()):
        if path.name == ".gitkeep":
            continue
        delete_path(path)
        deleted += 1
    if deleted:
        logger.info("Cleared %d item(s) from %s", deleted, storage_dir)
    return deleted


def cleanup_job_video(job_id: str, storage_dir: Path | None = None) -> None:
    base_dir = storage_dir or settings.video_storage_path
    session = session_store.get(job_id)
    if session and session.source == JobSource.PIXABAY:
        delete_pixabay_job_files(job_id, base_dir)
        return
    if session and session.video_path:
        delete_video_file(session.video_path)
    delete_job_storage_files(job_id, base_dir)


def discard_job(job_id: str) -> None:
    cleanup_job_video(job_id)
    session_store.remove(job_id)


def cleanup_stale_sessions(ttl_seconds: float) -> int:
    removed = 0
    for session in session_store.list_stale(ttl_seconds):
        cleanup_job_video(session.job_id)
        session_store.remove(session.job_id)
        removed += 1
        logger.info("Removed stale session: %s", session.job_id)
    return removed
