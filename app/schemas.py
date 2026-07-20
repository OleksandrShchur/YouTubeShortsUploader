from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class JobStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    DECLINED = "declined"


class JobMode(str, Enum):
    AWAITING_URL = "awaiting_url"
    AWAITING_MODIFIED_JSON = "awaiting_modified_json"
    PROCESSING = "processing"


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


class JobSession(BaseModel):
    job_id: str
    chat_id: int
    twitter_url: str
    video_path: str
    metadata: Optional[ShortsMetadata] = None
    status: JobStatus = JobStatus.PENDING_REVIEW
    mode: JobMode = JobMode.AWAITING_URL
    review_message_id: Optional[int] = None
    created_at: float
    updated_at: float
