import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SESSION_SECRET", "test-session-secret-32-chars-minimum-value")
os.environ.setdefault("AWS_BEARER_TOKEN_BEDROCK", "test-token")
os.environ.setdefault("AWS_PROFILE_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

from botocore.exceptions import ClientError

from services.ai.core import bedrock_service, gemini_service


class TestAiExhaustionAndTimeouts(unittest.TestCase):
    def setUp(self):
        gemini_service._model_cooldowns.clear()

    def test_is_rotatable_model_error_handles_bedrock_and_timeouts(self):
        throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "Converse")
        too_many = ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "Busy"}}, "Converse")
        self.assertTrue(gemini_service.is_rotatable_model_error(throttled))
        self.assertTrue(gemini_service.is_rotatable_model_error(too_many))
        self.assertTrue(gemini_service.is_rotatable_model_error(TimeoutError("hung")))
        self.assertFalse(gemini_service.is_rotatable_model_error(ValueError("bad payload")))

    def test_model_cooldown_blocks_and_recovers(self):
        model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        self.assertTrue(gemini_service.model_is_available(model))
        gemini_service.record_model_failure(model, ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "Converse"))
        self.assertFalse(gemini_service.model_is_available(model))
        gemini_service._model_cooldowns[model] = 0.0
        self.assertTrue(gemini_service.model_is_available(model))

    def test_bedrock_client_sets_timeouts_and_single_retry(self):
        adapter = bedrock_service.BedrockClientAdapter()
        with patch("services.ai.core.bedrock_service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()
            adapter.get_boto_client()
            cfg = mock_boto.call_args.kwargs["config"]
            self.assertEqual(cfg.connect_timeout, 10)
            self.assertEqual(cfg.read_timeout, 90)
            self.assertEqual(cfg.retries, {"max_attempts": 1, "mode": "standard"})

    def test_bedrock_stream_respects_timeout(self):
        adapter = bedrock_service.BedrockClientAdapter()
        fake_boto = MagicMock()

        def slow_stream(**_kwargs):
            import time
            time.sleep(0.3)
            return {"stream": []}

        fake_boto.converse_stream = slow_stream
        adapter._boto_client = fake_boto

        async def run():
            with patch.dict(os.environ, {"BEDROCK_STREAM_TIMEOUT_SEC": "0.05"}):
                with self.assertRaises(asyncio.TimeoutError):
                    await adapter.aio.models.generate_content_stream(model="model-a", contents="hi")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
