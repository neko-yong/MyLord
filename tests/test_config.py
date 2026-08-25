import unittest
from unittest.mock import patch

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
                "ADMIN_CONSOLE_ROUTE_KEY": "console-test",
                "ADMIN_MAINTENANCE_SECRET": "maintenance-test",
            },
        )

        self.assertTrue(settings.llm_ready)
        self.assertTrue(settings.admin_create_ready)
        self.assertTrue(settings.admin_console_ready)
        self.assertEqual(settings.database_url, "postgresql://environment-value")

    def test_missing_values_are_not_reported_ready(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = load_settings({}, {})

        self.assertFalse(bool(settings.database_url))
        self.assertFalse(settings.llm_ready)
        self.assertFalse(settings.admin_create_ready)
        self.assertFalse(settings.admin_console_ready)
        self.assertFalse(settings.dev_mode)
        self.assertEqual(settings.llm_mode, "real")
        self.assertEqual(settings.dev_database_mode, "postgres")

    def test_dev_mode_accepts_only_explicit_boolean_values(self):
        truthy = (True, 1, "1", "true", "TRUE", "yes", " YES ")
        falsey = (False, 0, "0", "false", "FALSE", "no", " NO ")

        for value in truthy:
            with self.subTest(value=value):
                settings = load_settings({"DEV_MODE": value}, {})
                self.assertTrue(settings.dev_mode)
                self.assertEqual(settings.llm_mode, "mock")
                self.assertEqual(settings.dev_database_mode, "local")

        for value in falsey:
            with self.subTest(value=value):
                settings = load_settings({"DEV_MODE": value}, {})
                self.assertFalse(settings.dev_mode)
                self.assertEqual(settings.llm_mode, "real")
                self.assertEqual(settings.dev_database_mode, "postgres")

        for value in ("on", "off", "enabled", "disabled", "2"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    load_settings({"DEV_MODE": value}, {})

    def test_production_forces_real_llm_even_if_mock_is_configured(self):
        settings = load_settings(
            {
                "DEV_MODE": "false",
                "LLM_MODE": "mock",
                "DEV_DATABASE_MODE": "local",
            },
            {},
        )

        self.assertFalse(settings.dev_mode)
        self.assertEqual(settings.llm_mode, "real")
        self.assertEqual(settings.dev_database_mode, "postgres")

    def test_dev_llm_mode_is_strict_and_defaults_to_mock(self):
        self.assertEqual(
            load_settings({"DEV_MODE": "true"}, {}).llm_mode,
            "mock",
        )
        self.assertEqual(
            load_settings(
                {"DEV_MODE": "true", "LLM_MODE": "real"},
                {},
            ).llm_mode,
            "real",
        )
        with self.assertRaises(ValueError):
            load_settings(
                {"DEV_MODE": "true", "LLM_MODE": "surprise"},
                {},
            )

    def test_dev_database_mode_defaults_local_and_maps_legacy_real(self):
        self.assertEqual(
            load_settings({"DEV_MODE": "true"}, {}).dev_database_mode,
            "local",
        )
        self.assertEqual(
            load_settings(
                {"DEV_MODE": "true", "DEV_DATABASE_MODE": "postgres"},
                {},
            ).dev_database_mode,
            "postgres",
        )
        self.assertEqual(
            load_settings(
                {"DEV_MODE": "true", "DEV_DATABASE_MODE": "real"},
                {},
            ).dev_database_mode,
            "postgres",
        )
        with self.assertRaises(ValueError):
            load_settings(
                {"DEV_MODE": "true", "DEV_DATABASE_MODE": "sqlite"},
                {},
            )

    def test_admin_secret_comparison(self):
        self.assertTrue(secure_secret_matches("correct", "correct"))
        self.assertFalse(secure_secret_matches("wrong", "correct"))
        self.assertFalse(secure_secret_matches("", "correct"))

    def test_admin_console_secrets_are_redacted_from_settings_repr(self):
        settings = load_settings(
            {
                "ADMIN_CONSOLE_ROUTE_KEY": "ROUTE_CANARY_92X",
                "ADMIN_MAINTENANCE_SECRET": "SECRET_CANARY_31K",
            },
            {},
        )

        self.assertTrue(settings.admin_console_ready)
        self.assertNotIn("ROUTE_CANARY_92X", repr(settings))
        self.assertNotIn("SECRET_CANARY_31K", repr(settings))


if __name__ == "__main__":
    unittest.main()
