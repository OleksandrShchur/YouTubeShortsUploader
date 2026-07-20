import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from app.config import settings
from app.schemas import ShortsMetadata
from app.utils.metadata_rules import build_display_title

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubeUploadError(Exception):
    pass


class YouTubeUploader:
    def __init__(self) -> None:
        self._service = None

    def _get_service(self):
        if self._service is None:
            self._service = build("youtube", "v3", credentials=_load_credentials())
        return self._service

    def upload_short(
        self,
        video_path: Path,
        metadata: ShortsMetadata,
    ) -> dict:
        if not video_path.exists():
            raise YouTubeUploadError(f"Video file not found: {video_path}")

        title = build_display_title(metadata)
        body = {
            "snippet": {
                "title": title,
                "description": metadata.description,
                "tags": metadata.shorts_tags,
                "categoryId": settings.youtube_category_id,
            },
            "status": {
                "privacyStatus": settings.youtube_privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(
            str(video_path),
            mimetype="video/mp4",
            resumable=True,
            chunksize=1024 * 1024,
        )

        request = (
            self._get_service()
            .videos()
            .insert(part="snippet,status", body=body, media_body=media)
        )

        response = None
        try:
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.info("YouTube upload progress: %s%%", int(status.progress() * 100))
        except Exception as exc:
            logger.exception("YouTube upload failed")
            raise YouTubeUploadError(f"YouTube upload failed: {exc}") from exc

        if not response:
            raise YouTubeUploadError("YouTube upload returned no response.")

        return response


def _load_credentials() -> Credentials:
    token_path = settings.youtube_token_file
    secrets_path = settings.youtube_client_secrets_file

    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_credentials(creds)
        return creds

    if not secrets_path.exists():
        raise YouTubeUploadError(
            f"YouTube OAuth client secrets not found at {secrets_path}. "
            "Download OAuth credentials from Google Cloud Console."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    creds = flow.run_local_server(port=0)
    _save_credentials(creds)
    return creds


def _save_credentials(creds: Credentials) -> None:
    token_path = settings.youtube_token_file
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
