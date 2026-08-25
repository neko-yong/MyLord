import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import integration_config


class IntegrationConfigTests(unittest.TestCase):
    def test_environment_takes_precedence(self):
        secrets = Mock()
        secrets.get.side_effect = AssertionError("secrets fallback used")
        with (
            patch.dict(os.environ, {"TEST_DATABASE_URL": "env-test-url"}),
            patch.object(
                integration_config,
                "st",
                SimpleNamespace(secrets=secrets),
            ),
        ):
            value, source = integration_config.load_test_database_url()
        self.assertEqual(value, "env-test-url")
        self.assertEqual(source, "ENV")

    def test_streamlit_secrets_are_the_fallback(self):
        with (
            patch.dict(os.environ, {"TEST_DATABASE_URL": ""}),
            patch.object(
                integration_config,
                "st",
                SimpleNamespace(
                    secrets={"TEST_DATABASE_URL": "secret-test-url"}
                ),
            ),
        ):
            value, source = integration_config.load_test_database_url()
        self.assertEqual(value, "secret-test-url")
        self.assertEqual(source, "STREAMLIT_SECRETS")

    def test_missing_test_database_is_unavailable(self):
        with (
            patch.dict(os.environ, {"TEST_DATABASE_URL": ""}),
            patch.object(
                integration_config,
                "st",
                SimpleNamespace(secrets={}),
            ),
        ):
            value, source = integration_config.load_test_database_url()
        self.assertIsNone(value)
        self.assertEqual(source, "NONE")


if __name__ == "__main__":
    unittest.main()
