import time
import uuid
from typing import Any, Optional

from app.schemas import (
    ChatFlow,
    JobMode,
    JobSession,
    JobSource,
    JobStatus,
    ReviewStage,
    ShortsMetadata,
)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, JobSession] = {}
        self._chat_active_job: dict[int, str] = {}
        self._chat_flow: dict[int, ChatFlow] = {}

    def get_chat_flow(self, chat_id: int) -> ChatFlow:
        return self._chat_flow.get(chat_id, ChatFlow.IDLE)

    def set_chat_flow(self, chat_id: int, flow: ChatFlow) -> None:
        if flow == ChatFlow.IDLE:
            self._chat_flow.pop(chat_id, None)
        else:
            self._chat_flow[chat_id] = flow

    def create_job(
        self,
        chat_id: int,
        video_path: str,
        *,
        job_id: str | None = None,
        source: JobSource = JobSource.TWITTER,
        twitter_url: str | None = None,
        review_stage: ReviewStage = ReviewStage.METADATA,
        video_prompts: dict[str, Any] | None = None,
        pixabay_phrase: str | None = None,
        pixabay_used_ids: list[int] | None = None,
        pixabay_used_audio_ids: list[int] | None = None,
        pixabay_meta: dict[str, Any] | None = None,
    ) -> JobSession:
        now = time.time()
        resolved_job_id = job_id or uuid.uuid4().hex[:12]
        session = JobSession(
            job_id=resolved_job_id,
            chat_id=chat_id,
            source=source,
            twitter_url=twitter_url,
            video_path=video_path,
            status=JobStatus.PENDING_REVIEW,
            mode=JobMode.PROCESSING,
            review_stage=review_stage,
            video_prompts=video_prompts,
            pixabay_phrase=pixabay_phrase,
            pixabay_used_ids=list(pixabay_used_ids or []),
            pixabay_used_audio_ids=list(pixabay_used_audio_ids or []),
            pixabay_meta=pixabay_meta,
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

    def update_video(
        self,
        job_id: str,
        video_path: str,
        *,
        video_prompts: dict[str, Any] | None = None,
        review_stage: ReviewStage = ReviewStage.VIDEO,
        mode: JobMode = JobMode.AWAITING_URL,
        review_message_id: int | None = None,
        pixabay_phrase: str | None = None,
        pixabay_used_ids: list[int] | None = None,
        pixabay_used_audio_ids: list[int] | None = None,
        pixabay_meta: dict[str, Any] | None = None,
    ) -> JobSession:
        session = self._require(job_id)
        session.video_path = video_path
        session.review_stage = review_stage
        session.mode = mode
        session.status = JobStatus.PENDING_REVIEW
        session.metadata = None
        session.updated_at = time.time()
        if video_prompts is not None:
            session.video_prompts = video_prompts
        if review_message_id is not None:
            session.review_message_id = review_message_id
        if pixabay_phrase is not None:
            session.pixabay_phrase = pixabay_phrase
        if pixabay_used_ids is not None:
            session.pixabay_used_ids = list(pixabay_used_ids)
        if pixabay_used_audio_ids is not None:
            session.pixabay_used_audio_ids = list(pixabay_used_audio_ids)
        if pixabay_meta is not None:
            session.pixabay_meta = pixabay_meta
        self._sessions[job_id] = session
        return session

    def update_metadata(
        self,
        job_id: str,
        metadata: ShortsMetadata,
        review_message_id: Optional[int] = None,
        mode: JobMode = JobMode.AWAITING_URL,
        review_stage: ReviewStage = ReviewStage.METADATA,
    ) -> JobSession:
        session = self._require(job_id)
        session.metadata = metadata
        session.mode = mode
        session.review_stage = review_stage
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

    def set_review_stage(self, job_id: str, review_stage: ReviewStage) -> JobSession:
        session = self._require(job_id)
        session.review_stage = review_stage
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
        self.set_chat_flow(session.chat_id, ChatFlow.IDLE)
        return session

    def remove(self, job_id: str) -> Optional[JobSession]:
        session = self._sessions.pop(job_id, None)
        if session and self._chat_active_job.get(session.chat_id) == job_id:
            del self._chat_active_job[session.chat_id]
        if session:
            self.set_chat_flow(session.chat_id, ChatFlow.IDLE)
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
