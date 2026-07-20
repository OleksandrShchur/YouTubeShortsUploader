import logging
from pathlib import Path

from app.session_store import session_store

logger = logging.getLogger(__name__)


def delete_video_file(video_path: str | Path) -> None:
    path = Path(video_path)
    if path.exists() and path.is_file():
        path.unlink()
        logger.info("Deleted video file: %s", path)


def cleanup_job_video(job_id: str) -> None:
    session = session_store.get(job_id)
    if not session:
        return
    delete_video_file(session.video_path)


def discard_job(job_id: str) -> None:
    cleanup_job_video(job_id)
    session_store.remove(job_id)


def cleanup_stale_sessions(ttl_seconds: float) -> int:
    removed = 0
    for session in session_store.list_stale(ttl_seconds):
        delete_video_file(session.video_path)
        session_store.remove(session.job_id)
        removed += 1
        logger.info("Removed stale session: %s", session.job_id)
    return removed
