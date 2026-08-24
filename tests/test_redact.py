from __future__ import annotations

import unittest

from qa_sentinel.redact import redact, redact_text


class RedactionTests(unittest.TestCase):
    def test_redacts_sensitive_mapping_keys_recursively(self) -> None:
        value = {
            "headers": {"Authorization": "Bearer abc123", "Accept": "application/json"},
            "nested": {"api_key": "secret"},
        }
        result = redact(value)
        self.assertEqual(result["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(result["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(result["headers"]["Accept"], "application/json")

    def test_redacts_bearer_and_query_credentials(self) -> None:
        text = "GET /users?token=abcd1234 Authorization: Bearer xyz.123"
        result = redact_text(text)
        self.assertNotIn("abcd1234", result)
        self.assertNotIn("xyz.123", result)
        self.assertEqual(result.count("[REDACTED]"), 2)

    def test_redacts_known_secret_in_free_text(self) -> None:
        result = redact_text("upstream said credential-987 expired", ("credential-987",))
        self.assertEqual(result, "upstream said [REDACTED] expired")


if __name__ == "__main__":
    unittest.main()

