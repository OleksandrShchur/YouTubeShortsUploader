import logging
from pathlib import Path

from google.auth.exceptions import RefreshError
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
        self._token: str | None = None

    def invalidate(self) -> None:
        self._service = None
        self._token = None

    def _get_service(self):
        creds = load_credentials()
        if self._service is None or self._token != creds.token:
            self._service = build("youtube", "v3", credentials=creds)
            self._token = creds.token
        return self._service

    def upload_short(
        self,
        video_path: Path | str,
        metadata: ShortsMetadata,
    ) -> dict:
        video_path = Path(video_path)
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


def load_credentials() -> Credentials:
    """Load OAuth credentials, refreshing the access token when needed."""
    token_path = settings.youtube_token_file
    secrets_path = settings.youtube_client_secrets_file

    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        _refresh_and_save(creds)
        return creds

    if not secrets_path.exists():
        raise YouTubeUploadError(
            f"YouTube OAuth client secrets not found at {secrets_path}. "
            "Download OAuth credentials from Google Cloud Console."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    _save_credentials(creds)
    return creds


def refresh_credentials() -> Credentials:
    """Force-refresh the access token using the stored refresh token and persist it."""
    token_path = settings.youtube_token_file
    if not token_path.exists():
        raise YouTubeUploadError(
            f"YouTube token file not found at {token_path}. "
            "Complete OAuth once and deploy secrets/youtube_token.json."
        )

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.refresh_token:
        raise YouTubeUploadError(
            "YouTube token file has no refresh_token. Re-run OAuth with offline access."
        )

    _refresh_and_save(creds)
    return creds


def _refresh_and_save(creds: Credentials) -> None:
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        raise YouTubeUploadError(
            "YouTube token refresh failed (invalid_grant). The refresh token is expired "
            "or revoked. Re-run browser OAuth locally, update the deployed token, and "
            "set the Google Cloud OAuth consent screen to In production so refresh "
            "tokens no longer expire after 7 days."
        ) from exc
    _save_credentials(creds)
    logger.info(
        "YouTube OAuth access token refreshed (expiry=%s)",
        creds.expiry.isoformat() if creds.expiry else "unknown",
    )


def _save_credentials(creds: Credentials) -> None:
    token_path = settings.youtube_token_file
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
