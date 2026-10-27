import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from google.genai import errors
from tenacity import Retrying

# Import settings without requiring developer credentials or a local .env file.
with patch.dict(os.environ, {
    "TELEGRAM_BOT_TOKEN": "test-token",
    "ADMIN_CHAT_ID": "123",
    "GEMINI_API_KEY": "test-key",
}):
    from app.services import gemini_client


class GeminiRetryTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            gemini_api_key="test-key",
            gemini_model="primary-model",
            gemini_fallback_model="fallback-model",
            generate_shorts_target_duration_seconds=12,
            generate_shorts_max_duration_seconds=20,
        )
        self.sdk = Mock()
        self.sleep = Mock()
        self.sdk.models.generate_content.return_value = SimpleNamespace(text=json.dumps({
            "scene_summary": "A cozy rainy scene",
            "clips": [{"prompt": "Rain outside a warm cabin"}],
        }))
        self.sdk.files.upload.return_value = SimpleNamespace(
            name="files/video", uri="https://example.com/video",
            mime_type="video/mp4", state=SimpleNamespace(name="ACTIVE"),
        )
        patches = [
            patch.object(gemini_client, "settings", self.settings),
            patch.object(gemini_client.genai, "Client", return_value=self.sdk),
            patch.object(gemini_client, "Retrying", side_effect=(
                lambda **kwargs: Retrying(sleep=self.sleep, **kwargs)
            )),
            patch.object(gemini_client, "logger"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = gemini_client.GeminiMetadataClient()

    def server_error(self, code=503):
        return errors.ServerError(code, {"error": {"message": "Temporarily unavailable"}})

    def called_models(self):
        return [call.kwargs["model"] for call in self.sdk.models.generate_content.call_args_list]

    def test_primary_success_does_not_retry(self):
        plan = self.client.generate_video_prompts()
        self.assertEqual(plan.scene_summary, "A cozy rainy scene")
        self.assertEqual(self.called_models(), ["primary-model"])
        self.sleep.assert_not_called()

    def test_transient_server_errors_recover_on_primary(self):
        response = self.sdk.models.generate_content.return_value
        self.sdk.models.generate_content.side_effect = [
            self.server_error(500), self.server_error(503), self.server_error(502), response,
        ]
        self.client.generate_video_prompts()
        self.assertEqual(self.called_models(), ["primary-model"] * 4)
        delays = [call.args[0] for call in self.sleep.call_args_list]
        self.assertEqual(len(delays), 3)
        for delay, minimum, maximum in zip(delays, [1, 2, 4], [2, 3, 4]):
            self.assertGreaterEqual(delay, minimum)
            self.assertLessEqual(delay, maximum)

    def test_fallback_preserves_request_after_primary_retries(self):
        response = self.sdk.models.generate_content.return_value
        self.sdk.models.generate_content.side_effect = [self.server_error()] * 4 + [response]
        self.client.generate_video_prompts()
        self.assertEqual(self.called_models(), ["primary-model"] * 4 + ["fallback-model"])
        calls = self.sdk.models.generate_content.call_args_list
        for call in calls:
            self.assertEqual(call.kwargs["contents"], calls[0].kwargs["contents"])
            self.assertEqual(call.kwargs["config"], calls[0].kwargs["config"])
            self.assertEqual(call.kwargs["config"].http_options.retry_options.attempts, 1)
        self.assertEqual(calls[-1].kwargs["config"].response_mime_type, "application/json")
        self.assertEqual(calls[-1].kwargs["config"].temperature, 0.9)

    def test_fallback_has_its_own_retry_budget(self):
        response = self.sdk.models.generate_content.return_value
        self.sdk.models.generate_content.side_effect = [self.server_error()] * 7 + [response]
        self.client.generate_video_prompts()
        self.assertEqual(self.called_models(), ["primary-model"] * 4 + ["fallback-model"] * 4)
        self.assertEqual(self.sleep.call_count, 6)

    def test_exhausted_models_raise_domain_error_with_cause(self):
        error = self.server_error()
        self.sdk.models.generate_content.side_effect = error
        with self.assertRaises(gemini_client.GeminiMetadataError) as caught:
            self.client.generate_video_prompts()
        self.assertIs(caught.exception.__cause__, error)
        self.assertIn("primary-model, fallback-model", str(caught.exception))
        self.assertEqual(self.sdk.models.generate_content.call_count, 8)

    def test_client_errors_do_not_retry_or_fall_back(self):
        for code in [400, 401, 403, 404, 429]:
            with self.subTest(code=code):
                self.sdk.models.generate_content.reset_mock()
                error = errors.ClientError(code, {"error": {"message": "Request rejected"}})
                self.sdk.models.generate_content.side_effect = error
                with self.assertRaises(gemini_client.GeminiMetadataError) as caught:
                    self.client.generate_video_prompts()
                self.assertIs(caught.exception.__cause__, error)
                self.assertEqual(self.called_models(), ["primary-model"])
                self.sleep.assert_not_called()

    def test_fallback_quota_error_names_fallback_model(self):
        error = errors.ClientError(429, {"error": {"message": "Quota exceeded"}})
        self.sdk.models.generate_content.side_effect = [self.server_error()] * 4 + [error]
        with self.assertRaisesRegex(gemini_client.GeminiMetadataError, "quota exceeded.*fallback-model"):
            self.client.generate_video_prompts()
        self.assertEqual(self.sdk.models.generate_content.call_count, 5)

    def test_empty_or_duplicate_fallback_is_skipped(self):
        for fallback in ["", "  ", "primary-model", " primary-model "]:
            with self.subTest(fallback=fallback):
                self.settings.gemini_fallback_model = fallback
                self.sdk.models.generate_content.reset_mock()
                self.sdk.models.generate_content.side_effect = self.server_error()
                with self.assertRaises(gemini_client.GeminiMetadataError):
                    self.client.generate_video_prompts()
                self.assertEqual(self.called_models(), ["primary-model"] * 4)

    def test_invalid_json_does_not_retry_or_fall_back(self):
        self.sdk.models.generate_content.return_value = SimpleNamespace(text="invalid json")
        with self.assertRaisesRegex(gemini_client.GeminiMetadataError, "not valid JSON"):
            self.client.generate_video_prompts()
        self.assertEqual(self.called_models(), ["primary-model"])

    def test_unexpected_errors_are_not_retried(self):
        self.sdk.models.generate_content.side_effect = RuntimeError("Unexpected failure")
        with self.assertRaises(RuntimeError):
            self.client.generate_video_prompts()
        self.assertEqual(self.called_models(), ["primary-model"])

    def test_metadata_reuses_upload_and_cleans_up_after_fallback(self):
        response = SimpleNamespace(text=json.dumps({
            "title": "Rainy cabin", "description": "Relax by the rain",
            "viral_title_tags": ["cozy", "rain", "shorts"], "shorts_tags": ["ambient"],
        }))
        self.sdk.models.generate_content.side_effect = [self.server_error()] * 4 + [response]
        video = Mock(spec=Path)
        video.exists.return_value = True
        metadata = self.client.generate_metadata(video)
        self.assertEqual(metadata.title, "Rainy cabin")
        self.sdk.files.upload.assert_called_once_with(file=str(video))
        self.sdk.files.delete.assert_called_once_with(name="files/video")
        self.assertEqual(self.called_models(), ["primary-model"] * 4 + ["fallback-model"])
        calls = self.sdk.models.generate_content.call_args_list
        self.assertEqual(calls[0].kwargs["contents"], calls[-1].kwargs["contents"])
        self.assertEqual(calls[-1].kwargs["config"].temperature, 0.7)

    def test_metadata_cleans_up_after_both_models_fail(self):
        self.sdk.models.generate_content.side_effect = self.server_error()
        video = Mock(spec=Path)
        video.exists.return_value = True
        with self.assertRaises(gemini_client.GeminiMetadataError):
            self.client.generate_metadata(video)
        self.sdk.files.upload.assert_called_once()
        self.sdk.files.delete.assert_called_once_with(name="files/video")
        self.assertEqual(self.sdk.models.generate_content.call_count, 8)


if __name__ == "__main__":
    unittest.main()
