import logging
from pathlib import Path

from huggingface_hub import InferenceClient

from app.config import settings

logger = logging.getLogger(__name__)


class HuggingFaceVideoError(Exception):
    pass


class HuggingFaceVideoClient:
    def __init__(self) -> None:
        if not settings.hf_token:
            raise HuggingFaceVideoError(
                "HF_TOKEN is not configured. Add a Hugging Face token with "
                "Inference Providers permission to .env."
            )
        provider = settings.hf_provider.strip() or "auto"
        self._client = InferenceClient(
            provider=provider,
            api_key=settings.hf_token,
        )
        self.video_model = settings.hf_video_model
        self.i2v_model = settings.hf_i2v_model

    def text_to_video(
        self,
        prompt: str,
        output_path: Path,
        *,
        negative_prompt: str | None = None,
        num_frames: int | None = None,
        seed: int | None = None,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict = {
            "model": self.video_model,
        }
        if negative_prompt:
            kwargs["negative_prompt"] = negative_prompt
        if num_frames is not None:
            kwargs["num_frames"] = num_frames
        if seed is not None:
            kwargs["seed"] = seed

        try:
            video_bytes = self._client.text_to_video(prompt, **kwargs)
        except Exception as exc:
            raise HuggingFaceVideoError(
                f"text_to_video failed for model '{self.video_model}': {exc}"
            ) from exc

        if not video_bytes:
            raise HuggingFaceVideoError("Hugging Face returned empty video bytes.")

        output_path.write_bytes(video_bytes)
        logger.info("Wrote text-to-video clip to %s (%d bytes)", output_path, len(video_bytes))
        return output_path

    def image_to_video(
        self,
        image_path: Path,
        prompt: str,
        output_path: Path,
        *,
        negative_prompt: str | None = None,
        num_frames: int | None = None,
        seed: int | None = None,
    ) -> Path:
        if not self.i2v_model:
            raise HuggingFaceVideoError("HF_I2V_MODEL is not configured.")
        if not image_path.exists():
            raise HuggingFaceVideoError(f"Conditioning image not found: {image_path}")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict = {
            "model": self.i2v_model,
            "prompt": prompt,
        }
        if negative_prompt:
            kwargs["negative_prompt"] = negative_prompt
        if num_frames is not None:
            kwargs["num_frames"] = num_frames
        if seed is not None:
            kwargs["seed"] = seed

        try:
            video_bytes = self._client.image_to_video(str(image_path), **kwargs)
        except Exception as exc:
            raise HuggingFaceVideoError(
                f"image_to_video failed for model '{self.i2v_model}': {exc}"
            ) from exc

        if not video_bytes:
            raise HuggingFaceVideoError("Hugging Face returned empty I2V video bytes.")

        output_path.write_bytes(video_bytes)
        logger.info("Wrote image-to-video clip to %s (%d bytes)", output_path, len(video_bytes))
        return output_path
