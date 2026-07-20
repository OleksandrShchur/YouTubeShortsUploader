import time
import uuid
from typing import Optional

from app.schemas import JobMode, JobSession, JobStatus, ShortsMetadata


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, JobSession] = {}
        self._chat_active_job: dict[int, str] = {}

    def create_job(
        self,
        chat_id: int,
        twitter_url: str,
        video_path: str,
        job_id: str | None = None,
    ) -> JobSession:
        now = time.time()
        resolved_job_id = job_id or uuid.uuid4().hex[:12]
        session = JobSession(
            job_id=resolved_job_id,
            chat_id=chat_id,
            twitter_url=twitter_url,
            video_path=video_path,
            status=JobStatus.PENDING_REVIEW,
            mode=JobMode.PROCESSING,
            created_at=now,
            updated_at=now,
        )
        self._sessions[resolved_job_id] = session
        self._chat_active_job[chat_id] = resolved_job_id
        return session

    def get(self, job_id: str) -> Optional[JobSession]:
        return self._sessions.get(job_id)

    def get_active_for_chat(self, chat_id: int) -> Optional[JobSession]:
        job_id = self._chat_active_job.get(chat_id)
        if not job_id:
            return None
        return self._sessions.get(job_id)

    def update_metadata(
        self,
        job_id: str,
        metadata: ShortsMetadata,
        review_message_id: Optional[int] = None,
        mode: JobMode = JobMode.AWAITING_URL,
    ) -> JobSession:
        session = self._require(job_id)
        session.metadata = metadata
        session.mode = mode
        session.status = JobStatus.PENDING_REVIEW
        session.updated_at = time.time()
        if review_message_id is not None:
            session.review_message_id = review_message_id
        self._sessions[job_id] = session
        return session

    def set_mode(self, job_id: str, mode: JobMode) -> JobSession:
        session = self._require(job_id)
        session.mode = mode
        session.updated_at = time.time()
        self._sessions[job_id] = session
        return session

    def complete(self, job_id: str, status: JobStatus) -> JobSession:
        session = self._require(job_id)
        session.status = status
        session.updated_at = time.time()
        self._sessions[job_id] = session
        if self._chat_active_job.get(session.chat_id) == job_id:
            del self._chat_active_job[session.chat_id]
        return session

    def remove(self, job_id: str) -> Optional[JobSession]:
        session = self._sessions.pop(job_id, None)
        if session and self._chat_active_job.get(session.chat_id) == job_id:
            del self._chat_active_job[session.chat_id]
        return session

    def list_stale(self, ttl_seconds: float) -> list[JobSession]:
        cutoff = time.time() - ttl_seconds
        return [
            session
            for session in self._sessions.values()
            if session.updated_at < cutoff
            and session.status == JobStatus.PENDING_REVIEW
        ]

    def _require(self, job_id: str) -> JobSession:
        session = self._sessions.get(job_id)
        if not session:
            raise KeyError(f"Job not found: {job_id}")
        return session


session_store = SessionStore()
