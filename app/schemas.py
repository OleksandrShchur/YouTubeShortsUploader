from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class JobStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    DECLINED = "declined"


class JobMode(str, Enum):
    AWAITING_URL = "awaiting_url"
    AWAITING_MODIFIED_JSON = "awaiting_modified_json"
    PROCESSING = "processing"


class ChatFlow(str, Enum):
    IDLE = "idle"
    TWITTER = "twitter"
    HUGGING_FACE = "hugging_face"
    PIXABAY = "pixabay"


class JobSource(str, Enum):
    TWITTER = "twitter"
    HUGGING_FACE = "huggingface"
    PIXABAY = "pixabay"


class ReviewStage(str, Enum):
    VIDEO = "video"
    METADATA = "metadata"


class ShortsMetadata(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    description: str = Field(..., min_length=1)
    viral_title_tags: list[str] = Field(..., min_length=3, max_length=4)
    shorts_tags: list[str] = Field(..., min_length=1)

    @field_validator("viral_title_tags", "shorts_tags", mode="before")
    @classmethod
    def strip_tags(cls, value: list[str]) -> list[str]:
        if not isinstance(value, list):
            raise TypeError("tags must be a list")
        cleaned: list[str] = []
        for tag in value:
            tag_str = str(tag).strip().lstrip("#")
            if tag_str and tag_str not in cleaned:
                cleaned.append(tag_str)
        return cleaned

    @field_validator("title", "description", mode="before")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return str(value).strip()


class VideoClipPrompt(BaseModel):
    index: int
    duration_hint_seconds: float = Field(..., ge=1, le=15)
    prompt: str = Field(..., min_length=1)


class VideoPromptPlan(BaseModel):
    scene_summary: str = Field(..., min_length=1)
    target_duration_seconds: float = Field(..., ge=8, le=15)
    negative_prompt: str = Field(..., min_length=1)
    clips: list[VideoClipPrompt] = Field(..., min_length=1, max_length=4)


class JobSession(BaseModel):
    job_id: str
    chat_id: int
    source: JobSource = JobSource.TWITTER
    twitter_url: Optional[str] = None
    video_path: str
    metadata: Optional[ShortsMetadata] = None
    status: JobStatus = JobStatus.PENDING_REVIEW
    mode: JobMode = JobMode.AWAITING_URL
    review_stage: ReviewStage = ReviewStage.METADATA
    review_message_id: Optional[int] = None
    video_prompts: Optional[dict[str, Any]] = None
    pixabay_phrase: Optional[str] = None
    pixabay_used_ids: list[int] = Field(default_factory=list)
    pixabay_used_audio_ids: list[int] = Field(default_factory=list)
    pixabay_meta: Optional[dict[str, Any]] = None
    created_at: float
    updated_at: float
