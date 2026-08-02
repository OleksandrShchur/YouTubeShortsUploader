from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str
    admin_chat_id: int
    # Public HTTPS base URL for Telegram webhooks (e.g. https://example.com).
    # When unset, the bot falls back to long polling (local development).
    telegram_webhook_url: str = ""
    telegram_webhook_path: str = "/telegram/webhook"
    telegram_webhook_secret: str = ""

    gemini_api_key: str
    gemini_model: str = "gemini-3.5-flash"

    youtube_client_secrets_file: Path = Path("secrets/client_secret.json")
    youtube_token_file: Path = Path("secrets/youtube_token.json")
    youtube_privacy_status: str = "private"
    youtube_category_id: str = "22"
    # How often to force-refresh the YouTube access token and notify the admin.
    youtube_token_refresh_hours: int = 24

    video_storage_dir: Path = Path("storage/videos")
    session_ttl_hours: int = 24

    pixabay_api_key: str = ""

    @property
    def video_storage_path(self) -> Path:
        path = self.video_storage_dir
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
