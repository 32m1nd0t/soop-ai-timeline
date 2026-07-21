import unittest
from unittest.mock import patch

from soop_timeline.services.ai_provider import (
    AI_PROVIDER_SPECS,
    AIUsage,
    AIRequestFailure,
    StructuredAIProvider,
    StructuredAIResponse,
    estimate_timeline_calls,
    classify_ai_error,
    normalize_ai_provider,
    strict_json_schema,
)


class FakeProvider(StructuredAIProvider):
    @property
    def provider_id(self):
        return "gemini"

    @property
    def unavailable_reason(self):
        return "" if self.api_key else "missing"

    def _perform_request(self, prompt, schema, *, purpose):
        self.last_request = (prompt, schema, purpose)
        return StructuredAIResponse({"status": "ok"}, 12, 3)


class AIProviderTests(unittest.TestCase):
    def test_provider_name_and_call_estimate_are_normalized(self):
        self.assertEqual(set(AI_PROVIDER_SPECS), {"gemini"})
        self.assertEqual(normalize_ai_provider("legacy-provider"), "gemini")
        self.assertEqual(normalize_ai_provider("unknown"), "gemini")
        self.assertEqual(estimate_timeline_calls(0), 2)
        self.assertEqual(estimate_timeline_calls(45 * 60), 2)
        self.assertGreater(estimate_timeline_calls(8 * 3600), 2)

    def test_schema_strips_additional_properties_for_nested_objects(self):
        # The Gemini API rejects ``additionalProperties`` with 400
        # INVALID_ARGUMENT, so it must be removed from every node, including
        # any that were provided on the input schema.
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        }
        strict = strict_json_schema(schema)
        self.assertNotIn("additionalProperties", strict)
        self.assertNotIn(
            "additionalProperties",
            strict["properties"]["items"]["items"],
        )
        # The input schema must not be mutated in place.
        self.assertIn("additionalProperties", schema)

    def test_usage_is_recorded_for_connection_test(self):
        provider = FakeProvider("key", "model")
        message = provider.test_connection(force=True)
        self.assertIn("연결 성공", message)
        self.assertEqual(provider.usage, AIUsage(calls=1, input_tokens=12, output_tokens=3))

    def test_errors_are_classified_without_retrying_auth_or_daily_quota(self):
        class ProviderError(RuntimeError):
            def __init__(self, code, message):
                super().__init__(message)
                self.code = code

        auth = classify_ai_error(ProviderError(401, "API key not valid"))
        quota = classify_ai_error(
            ProviderError(429, "GenerateRequestsPerDayPerProject quota exceeded")
        )
        rate = classify_ai_error(ProviderError(429, "too many requests"))
        self.assertEqual(auth.category, "auth")
        self.assertFalse(auth.retryable)
        self.assertEqual(quota.category, "quota")
        self.assertFalse(quota.retryable)
        self.assertEqual(rate.category, "rate_limit")
        self.assertTrue(rate.retryable)

    def test_rate_limit_honours_server_retry_delay(self):
        class ApiError(RuntimeError):
            def __init__(self):
                super().__init__("Resource has been exhausted (rate limit)")
                self.code = 429
                self.details = {
                    "error": {
                        "code": 429,
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "rate limit",
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                "retryDelay": "39s",
                            }
                        ],
                    }
                }

        info = classify_ai_error(ApiError())
        self.assertEqual(info.category, "rate_limit")
        self.assertTrue(info.retryable)
        # Waits at least as long as the server asked, capped at the backoff limit.
        self.assertGreaterEqual(info.retry_after_seconds, 39.0)
        self.assertLessEqual(info.retry_after_seconds, 60.0)

    def test_rate_limit_without_retry_info_uses_default_delay(self):
        class ProviderError(RuntimeError):
            def __init__(self, code, message):
                super().__init__(message)
                self.code = code

        rate = classify_ai_error(ProviderError(429, "too many requests"))
        self.assertEqual(rate.category, "rate_limit")
        self.assertGreater(rate.retry_after_seconds, 0.0)

    def test_transient_failure_retries_but_permanent_failure_stops(self):
        class SequenceProvider(FakeProvider):
            def __init__(self, errors):
                super().__init__("key", "model")
                self.errors = list(errors)
                self.calls = 0

            def _perform_request(self, prompt, schema, *, purpose):
                self.calls += 1
                if self.errors:
                    raise self.errors.pop(0)
                return StructuredAIResponse({"status": "ok"})

        transient = SequenceProvider([TimeoutError("timed out")])
        with patch("soop_timeline.services.ai_provider._interruptible_backoff"):
            self.assertEqual(
                transient.request_json("x", {"type": "object"}, lambda: False),
                {"status": "ok"},
            )
        self.assertEqual(transient.calls, 2)

        permanent = SequenceProvider([RuntimeError("API key not valid")])
        with self.assertRaises(AIRequestFailure):
            permanent.request_json("x", {"type": "object"}, lambda: False)
        self.assertEqual(permanent.calls, 1)


if __name__ == "__main__":
    unittest.main()
