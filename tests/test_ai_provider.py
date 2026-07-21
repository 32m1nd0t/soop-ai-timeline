import unittest

from soop_timeline.services.ai_provider import (
    AIUsage,
    StructuredAIProvider,
    StructuredAIResponse,
    estimate_timeline_calls,
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
        self.assertEqual(normalize_ai_provider("OPENAI"), "openai")
        self.assertEqual(normalize_ai_provider("unknown"), "gemini")
        self.assertEqual(estimate_timeline_calls(0), 2)
        self.assertEqual(estimate_timeline_calls(45 * 60), 2)
        self.assertGreater(estimate_timeline_calls(8 * 3600), 2)

    def test_schema_is_strict_for_nested_objects(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                }
            },
            "required": ["items"],
        }
        strict = strict_json_schema(schema)
        self.assertFalse(strict["additionalProperties"])
        self.assertFalse(strict["properties"]["items"]["items"]["additionalProperties"])
        self.assertNotIn("additionalProperties", schema)

    def test_usage_is_recorded_for_connection_test(self):
        provider = FakeProvider("key", "model")
        message = provider.test_connection(force=True)
        self.assertIn("연결 성공", message)
        self.assertEqual(provider.usage, AIUsage(calls=1, input_tokens=12, output_tokens=3))


if __name__ == "__main__":
    unittest.main()
