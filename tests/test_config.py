import unittest

from config import load_settings, secure_secret_matches


class SettingsTests(unittest.TestCase):
    def test_streamlit_secrets_take_precedence_over_environment(self):
        settings = load_settings(
            {
                "DATABASE_URL": "postgresql://secret-value",
                "LLM_MODEL": "secret-model",
            },
            {
                "DATABASE_URL": "postgresql://environment-value",
                "LLM_MODEL": "environment-model",
            },
        )

        self.assertEqual(settings.database_url, "postgresql://secret-value")
        self.assertEqual(settings.llm_model, "secret-model")

    def test_environment_is_used_as_fallback(self):
        settings = load_settings(
            {},
            {
                "DATABASE_URL": "postgresql://environment-value",
                "LLM_ENDPOINT": "https://provider.example/chat/completions",
                "LLM_MODEL": "test-model",
                "LLM_API_KEY": "test-key",
                "ADMIN_CREATE_SECRET": "admin-test",
            },
        )

        self.assertTrue(settings.llm_ready)
        self.assertTrue(settings.admin_create_ready)
        self.assertEqual(settings.database_url, "postgresql://environment-value")

    def test_missing_values_are_not_reported_ready(self):
        settings = load_settings({}, {})

        self.assertEqual(settings.database_url, "")
        self.assertFalse(settings.llm_ready)
        self.assertFalse(settings.admin_create_ready)

    def test_admin_secret_comparison(self):
        self.assertTrue(secure_secret_matches("correct", "correct"))
        self.assertFalse(secure_secret_matches("wrong", "correct"))
        self.assertFalse(secure_secret_matches("", "correct"))


if __name__ == "__main__":
    unittest.main()
