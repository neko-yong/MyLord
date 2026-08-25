import unittest
from unittest.mock import Mock, patch

import requests

from llm import LLMError, LLMResult, TASK_MAX_TOKENS, call_llm


ENDPOINT = "https://provider.example/v1/chat/completions"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
MODEL = "test-model"
API_KEY = "unit-test-secret-key"


def invoke(endpoint=ENDPOINT, max_tokens=None):
    return call_llm(
        endpoint=endpoint,
        model=MODEL,
        api_key=API_KEY,
        system_prompt="system",
        user_prompt="user",
        max_tokens=max_tokens,
    )


def completion_response(*, finish_reason="stop", usage=None, content="result"):
    response = Mock(ok=True, status_code=200)
    data = {
        "model": "response-model",
        "choices": [
            {
                "message": {
                    "content": content,
                    "reasoning_content": "must never be returned",
                },
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        data["usage"] = usage
    response.json.return_value = data
    return response


class LLMTests(unittest.TestCase):
    def test_task_budgets_are_specific(self):
        self.assertEqual(
            TASK_MAX_TOKENS,
            {
                "CONNECTION_TEST": 16,
                "DISPUTE_MAP": 3000,
                "INTERVENTION": 1000,
                "JUDGMENT_NORMAL": 6000,
                "JUDGMENT_SWAPPED": 6000,
                "META_JUDGMENT": 6000,
            },
        )

    @patch("llm.requests.post")
    def test_successful_chat_completion_returns_structured_result(self, post):
        post.return_value = completion_response()

        result = invoke()

        self.assertIsInstance(result, LLMResult)
        self.assertEqual(result.content, "result")
        self.assertEqual(result.model, "response-model")
        self.assertGreaterEqual(result.latency_ms, 0)
        self.assertNotIn("must never be returned", repr(result))

    @patch("llm.requests.post")
    def test_deepseek_payload_disables_thinking(self, post):
        post.return_value = completion_response()

        invoke(endpoint=DEEPSEEK_ENDPOINT)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    @patch("llm.requests.post")
    def test_non_deepseek_payload_omits_thinking(self, post):
        post.return_value = completion_response()

        invoke(endpoint=ENDPOINT)

        self.assertNotIn("thinking", post.call_args.kwargs["json"])

    @patch("llm.requests.post")
    def test_max_tokens_enters_payload(self, post):
        post.return_value = completion_response()

        invoke(max_tokens=3000)

        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 3000)

    @patch("llm.requests.post")
    def test_finish_reason_and_usage_are_parsed(self, post):
        post.return_value = completion_response(
            finish_reason="stop",
            usage={
                "prompt_tokens": 12,
                "completion_tokens": 34,
                "total_tokens": 46,
            },
        )

        result = invoke()

        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.prompt_tokens, 12)
        self.assertEqual(result.completion_tokens, 34)
        self.assertEqual(result.total_tokens, 46)

    @patch("llm.requests.post")
    def test_missing_usage_is_parsed_defensively(self, post):
        post.return_value = completion_response(usage=None)

        result = invoke()

        self.assertIsNone(result.prompt_tokens)
        self.assertIsNone(result.completion_tokens)
        self.assertIsNone(result.total_tokens)

    @patch("llm.time.perf_counter", side_effect=[10.0, 10.125])
    @patch("llm.requests.post")
    def test_length_finish_reason_retains_safe_telemetry(self, post, _timer):
        post.return_value = completion_response(
            finish_reason="length",
            usage={
                "prompt_tokens": 101,
                "completion_tokens": 6000,
                "total_tokens": 6101,
            },
        )

        with self.assertRaises(LLMError) as raised:
            invoke()

        error = raised.exception
        self.assertEqual(error.category, "truncated")
        self.assertEqual(error.finish_reason, "length")
        self.assertEqual(error.model, "response-model")
        self.assertEqual(error.prompt_tokens, 101)
        self.assertEqual(error.completion_tokens, 6000)
        self.assertEqual(error.total_tokens, 6101)
        self.assertEqual(error.latency_ms, 125.0)

    @patch("llm.requests.post")
    def test_truncated_error_does_not_include_response_content_or_secrets(
        self,
        post,
    ):
        database_url = "postgresql://private-database-credential"
        truncated_content = (
            f"Authorization: Bearer {API_KEY}\n"
            f"DATABASE_URL={database_url}\n"
            "partial private response"
        )
        post.return_value = completion_response(
            finish_reason="length",
            usage={"completion_tokens": 6000},
            content=truncated_content,
        )

        with self.assertRaises(LLMError) as raised:
            invoke()

        visible_error = str(raised.exception) + raised.exception.debug_summary()
        self.assertNotIn(API_KEY, visible_error)
        self.assertNotIn(database_url, visible_error)
        self.assertNotIn("Authorization", visible_error)
        self.assertNotIn("partial private response", visible_error)

    @patch("llm.requests.post")
    def test_auth_error_is_classified_and_key_is_redacted(self, post):
        response = Mock(ok=False, status_code=401)
        response.text = f"Authorization: Bearer {API_KEY}"
        post.return_value = response

        with self.assertRaises(LLMError) as raised:
            invoke()

        error = raised.exception
        self.assertEqual(error.category, "authentication")
        self.assertNotIn(API_KEY, error.debug_summary())
        self.assertIn("[REDACTED]", error.debug_summary())

    @patch("llm.requests.post", side_effect=requests.Timeout("too slow"))
    def test_timeout_is_classified(self, _post):
        with self.assertRaises(LLMError) as raised:
            invoke()

        self.assertEqual(raised.exception.category, "timeout")

    @patch("llm.requests.post")
    def test_incompatible_schema_is_classified_without_reasoning_leak(self, post):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {
            "reasoning_content": "private chain of thought",
            "output": "not chat completions",
        }
        post.return_value = response

        with self.assertRaises(LLMError) as raised:
            invoke()

        self.assertEqual(raised.exception.category, "response_schema")
        self.assertNotIn("private chain of thought", raised.exception.debug_summary())

    def test_credentials_in_endpoint_are_rejected_before_request(self):
        with self.assertRaises(LLMError) as raised:
            call_llm(
                endpoint="https://provider.example/chat/completions?api_key=secret",
                model=MODEL,
                api_key=API_KEY,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(raised.exception.category, "configuration")


if __name__ == "__main__":
    unittest.main()
