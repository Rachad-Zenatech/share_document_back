import unittest
from unittest.mock import MagicMock, patch

from services.ai.core.bedrock_service import (
    BedrockClientAdapter,
    BedrockResponse,
    clean_json_fences,
    get_bedrock_profile_id,
    get_bedrock_region,
    has_ai_key,
    is_bedrock_enabled,
    _build_converse_params,
    _extract_text_and_media,
)


class BedrockServiceUnitTests(unittest.TestCase):
    def test_clean_json_fences(self):
        self.assertEqual(clean_json_fences('{"key": "value"}'), '{"key": "value"}')
        self.assertEqual(clean_json_fences('```json\n{"key": "value"}\n```'), '{"key": "value"}')
        self.assertEqual(clean_json_fences('```\n[1, 2, 3]\n```'), '[1, 2, 3]')

    def test_environment_configuration_detection(self):
        with patch.dict("os.environ", {"AWS_BEARER_TOKEN_BEDROCK": "test-key", "AWS_PROFILE_ID": "us.claude-haiku", "AWS_AI_REGION": "ca-central-1"}, clear=True):
            self.assertTrue(is_bedrock_enabled())
            self.assertTrue(has_ai_key())
            self.assertEqual(get_bedrock_profile_id(), "us.claude-haiku")
            self.assertEqual(get_bedrock_region(), "ca-central-1")

        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(is_bedrock_enabled())
            self.assertFalse(has_ai_key())

    def test_response_shape_compatibility(self):
        resp = BedrockResponse("Hello world", usage={"inputTokens": 10, "outputTokens": 5})
        self.assertEqual(resp.text, "Hello world")
        self.assertEqual(resp.usage_metadata.prompt_token_count, 10)
        self.assertEqual(resp.usage_metadata.candidates_token_count, 5)
        self.assertEqual(resp.candidates[0].content.parts[0].text, "Hello world")

    def test_extract_text_and_media(self):
        blocks, _ = _extract_text_and_media(["Hello", "World"])
        self.assertEqual(blocks, [{"text": "Hello"}, {"text": "World"}])

        dummy_part = MagicMock()
        dummy_part.inline_data.data = b"image_bytes"
        dummy_part.inline_data.mime_type = "image/png"
        blocks, _ = _extract_text_and_media([dummy_part])
        self.assertEqual(blocks[0]["image"]["format"], "png")
        self.assertEqual(blocks[0]["image"]["source"]["bytes"], b"image_bytes")

    def test_build_converse_params(self):
        config = MagicMock()
        config.system_instruction = "Be helpful."
        config.response_mime_type = "application/json"
        config.max_output_tokens = 500

        with patch.dict("os.environ", {"AWS_PROFILE_ID": "us.claude-test"}, clear=True):
            params = _build_converse_params(None, "Hello", config)
            self.assertEqual(params["modelId"], "us.claude-test")
            self.assertEqual(params["messages"][0]["content"][0]["text"], "Hello")
            self.assertIn("Be helpful.", params["system"][0]["text"])
            self.assertIn("JSON", params["system"][0]["text"])
            self.assertEqual(params["inferenceConfig"]["maxTokens"], 500)


class BedrockClientAdapterTests(unittest.TestCase):
    def test_models_generate_content_mocked(self):
        adapter = BedrockClientAdapter()
        mock_boto = MagicMock()
        mock_boto.converse.return_value = {
            "output": {"message": {"content": [{"text": '{"result": "ok"}'}]}},
            "usage": {"inputTokens": 20, "outputTokens": 10},
        }
        adapter._boto_client = mock_boto

        config = MagicMock()
        config.response_mime_type = "application/json"
        config.response_schema = None

        resp = adapter.models.generate_content(contents="test prompt", config=config)
        self.assertEqual(resp.text, '{"result": "ok"}')
        self.assertEqual(resp.parsed, {"result": "ok"})
        self.assertEqual(resp.usage_metadata.prompt_token_count, 20)


if __name__ == "__main__":
    unittest.main()
