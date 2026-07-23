import logging
from pathlib import Path

from app.schemas import JobSource
from app.session_store import session_store

logger = logging.getLogger(__name__)


def delete_video_file(video_path: str | Path) -> None:
    path = Path(video_path)
    if path.exists() and path.is_file():
        path.unlink()
        logger.info("Deleted video file: %s", path)


def delete_pixabay_job_files(job_id: str, storage_dir: Path | None = None) -> None:
    """Delete muxed video plus silent/audio sidecars for a Pixabay job."""
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

    base_dir = storage_dir
    if base_dir is None and session and session.video_path:
        base_dir = Path(session.video_path).parent
    if base_dir is not None:
        paths.extend(
            [
                base_dir / f"{job_id}.mp4",
                base_dir / f"{job_id}_silent.mp4",
                base_dir / f"{job_id}_audio.mp3",
                base_dir / f"{job_id}_audio_tmp.mp3",
                base_dir / f"{job_id}_mux_tmp.mp4",
                base_dir / f"{job_id}.pending",
            ]
        )

    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        delete_video_file(path)


def clear_video_storage_dir(storage_dir: Path) -> int:
    """Delete all files in the video storage directory. Returns count deleted."""
    storage_dir.mkdir(parents=True, exist_ok=True)
    deleted = 0
    for path in storage_dir.iterdir():
        if path.is_file() and path.name != ".gitkeep":
            path.unlink()
            deleted += 1
            logger.info("Deleted video file: %s", path)
    if deleted:
        logger.info("Cleared %d file(s) from %s", deleted, storage_dir)
    return deleted


def cleanup_job_video(job_id: str) -> None:
    session = session_store.get(job_id)
    if not session:
        return
    if session.source == JobSource.PIXABAY:
        delete_pixabay_job_files(job_id)
        return
    delete_video_file(session.video_path)


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
