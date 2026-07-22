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

    gemini_api_key: str
    gemini_model: str = "gemini-3.5-flash"

    youtube_client_secrets_file: Path = Path("secrets/client_secret.json")
    youtube_token_file: Path = Path("secrets/youtube_token.json")
    youtube_privacy_status: str = "private"
    youtube_category_id: str = "22"

    video_storage_dir: Path = Path("storage/videos")
    session_ttl_hours: int = 24

    hf_token: str = ""
    hf_video_model: str = "Lightricks/LTX-Video-0.9.8-13B-distilled"
    hf_i2v_model: str = "Wan-AI/Wan2.2-I2V-A14B"
    hf_provider: str = "auto"
    hf_target_duration_seconds: float = 12.0

    @property
    def video_storage_path(self) -> Path:
        path = self.video_storage_dir
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
