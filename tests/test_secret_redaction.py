import io
import logging
import traceback
import unittest
from unittest.mock import Mock, patch

import requests
from psycopg import OperationalError

from config import load_settings
from db import Database, DatabaseUnavailable, create_postgres_pool
from llm import LLMError, call_llm
from secret_redaction import (
    REDACTED_DATABASE_URL,
    REDACTED_SECRET,
    REDACTED_TOKEN,
    install_postgres_pool_log_redaction,
    redact_database_url,
    redact_secrets,
)


FAKE_PASSWORD = "CANARY_DATABASE_PASSWORD_DO_NOT_PRINT_92841"
FAKE_DATABASE_URL = (
    "postgresql://fake_user:"
    f"{FAKE_PASSWORD}@example.invalid:5432/fake_db?sslmode=require"
)
FAKE_LLM_KEY = "fake-llm-key-CANARY-92841"
FAKE_ADMIN_SECRET = "fake-admin-secret-CANARY-92841"
FAKE_A_TOKEN = "A-fake_raw_token_CANARY_92841_value"
FAKE_B_TOKEN = "B-fake_raw_token_CANARY_92841_value"


class SecretRedactionTests(unittest.TestCase):
    def test_database_url_is_fully_redacted(self):
        self.assertEqual(
            redact_database_url(FAKE_DATABASE_URL),
            REDACTED_DATABASE_URL,
        )
        redacted = redact_secrets(f"connection={FAKE_DATABASE_URL}")
        self.assertEqual(redacted, f"connection={REDACTED_DATABASE_URL}")

    def test_unified_redaction_removes_all_fake_secrets(self):
        payload = "\n".join(
            (
                f"DATABASE_URL={FAKE_DATABASE_URL}",
                f"LLM_API_KEY={FAKE_LLM_KEY}",
                f"ADMIN_CREATE_SECRET={FAKE_ADMIN_SECRET}",
                f"Authorization: Bearer {FAKE_LLM_KEY}",
                FAKE_A_TOKEN,
                FAKE_B_TOKEN,
            )
        )
        redacted = redact_secrets(
            payload,
            (
                FAKE_LLM_KEY,
                FAKE_ADMIN_SECRET,
            ),
        )

        for forbidden in (
            FAKE_PASSWORD,
            "fake_user",
            "example.invalid",
            "fake_db",
            FAKE_LLM_KEY,
            FAKE_ADMIN_SECRET,
            FAKE_A_TOKEN,
            FAKE_B_TOKEN,
        ):
            self.assertNotIn(forbidden, redacted)
        self.assertIn(REDACTED_DATABASE_URL, redacted)
        self.assertIn(REDACTED_SECRET, redacted)
        self.assertIn(REDACTED_TOKEN, redacted)

    def test_settings_repr_excludes_sensitive_fields(self):
        settings = load_settings(
            {
                "DATABASE_URL": FAKE_DATABASE_URL,
                "LLM_ENDPOINT": "https://example.invalid/secret-path",
                "LLM_MODEL": "safe-model-name",
                "LLM_API_KEY": FAKE_LLM_KEY,
                "ADMIN_CREATE_SECRET": FAKE_ADMIN_SECRET,
            },
            {"DEV_MODE": "false"},
        )

        visible = repr(settings)
        for forbidden in (
            FAKE_DATABASE_URL,
            FAKE_PASSWORD,
            "example.invalid",
            FAKE_LLM_KEY,
            FAKE_ADMIN_SECRET,
        ):
            self.assertNotIn(forbidden, visible)
        self.assertIn("safe-model-name", visible)

    @patch("db.ConnectionPool")
    def test_pool_setup_exception_suppresses_raw_dsn(self, pool_class):
        pool_class.side_effect = ValueError(
            f"could not connect using {FAKE_DATABASE_URL}"
        )

        with self.assertRaises(DatabaseUnavailable) as raised:
            create_postgres_pool(FAKE_DATABASE_URL)

        visible = "".join(
            traceback.format_exception(raised.exception)
        )
        self.assertNotIn(FAKE_PASSWORD, visible)
        self.assertNotIn("fake_user", visible)
        self.assertNotIn("example.invalid", visible)
        self.assertIn("ValueError", visible)
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_query_exception_suppresses_raw_dsn(self):
        pool = Mock()
        pool.connection.side_effect = OperationalError(
            f"query failed using {FAKE_DATABASE_URL}"
        )
        database = Database(pool=pool)

        with self.assertRaises(DatabaseUnavailable) as raised:
            database.health_check()

        visible = "".join(
            traceback.format_exception(raised.exception)
        )
        self.assertNotIn(FAKE_PASSWORD, visible)
        self.assertNotIn("fake_user", visible)
        self.assertNotIn("example.invalid", visible)
        self.assertIn("OperationalError", visible)
        self.assertEqual(pool.connection.call_count, 2)
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_psycopg_pool_warning_suppresses_connection_metadata(self):
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        logger = logging.getLogger("psycopg.pool")
        install_postgres_pool_log_redaction()
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            logger.warning(
                "error connecting in %r: %s",
                "fake-pool",
                OperationalError(
                    f"connection failed using {FAKE_DATABASE_URL}"
                ),
            )
        finally:
            logger.removeHandler(handler)

        output = captured.getvalue()
        for forbidden in (
            FAKE_PASSWORD,
            "fake_user",
            "example.invalid",
            "fake_db",
        ):
            self.assertNotIn(forbidden, output)
        self.assertIn("connection details redacted", output)
        self.assertIn("OperationalError", output)

    @patch("llm.requests.post")
    def test_llm_exception_suppresses_all_fake_secrets(self, post):
        post.side_effect = requests.RequestException(
            " ".join(
                (
                    FAKE_DATABASE_URL,
                    f"LLM_API_KEY={FAKE_LLM_KEY}",
                    f"ADMIN_CREATE_SECRET={FAKE_ADMIN_SECRET}",
                    FAKE_A_TOKEN,
                    FAKE_B_TOKEN,
                )
            )
        )

        with self.assertRaises(LLMError) as raised:
            call_llm(
                endpoint="https://provider.invalid/chat/completions",
                model="fake-model",
                api_key=FAKE_LLM_KEY,
                system_prompt="system",
                user_prompt="user",
            )

        visible = "".join(
            traceback.format_exception(raised.exception)
        ) + raised.exception.debug_summary()
        for forbidden in (
            FAKE_PASSWORD,
            "fake_user",
            "example.invalid",
            "fake_db",
            FAKE_LLM_KEY,
            FAKE_ADMIN_SECRET,
            FAKE_A_TOKEN,
            FAKE_B_TOKEN,
        ):
            self.assertNotIn(forbidden, visible)
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_deliberate_failure_output_contains_no_canary(self):
        settings = load_settings(
            {
                "DATABASE_URL": FAKE_DATABASE_URL,
                "LLM_ENDPOINT": "https://example.invalid/secret-path",
                "LLM_MODEL": "safe-model-name",
                "LLM_API_KEY": FAKE_LLM_KEY,
                "ADMIN_CREATE_SECRET": FAKE_ADMIN_SECRET,
            },
            {"DEV_MODE": "false"},
        )
        safe_payload = redact_secrets(
            " ".join(
                (
                    FAKE_DATABASE_URL,
                    FAKE_LLM_KEY,
                    FAKE_ADMIN_SECRET,
                    FAKE_A_TOKEN,
                    FAKE_B_TOKEN,
                )
            ),
            (
                FAKE_LLM_KEY,
                FAKE_ADMIN_SECRET,
                FAKE_A_TOKEN,
                FAKE_B_TOKEN,
            ),
        )
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        logger = logging.getLogger("secret-redaction-canary")
        logger.addHandler(handler)
        logger.setLevel(logging.ERROR)
        try:
            logger.error("%s", safe_payload)

            class DeliberatePresenceFailure(unittest.TestCase):
                def runTest(self):
                    self.assertFalse(bool(settings.database_url))

            class DeliberateReprFailure(unittest.TestCase):
                def runTest(self):
                    self.assertEqual(
                        repr(settings),
                        "intentional mismatch",
                    )

            result = unittest.TextTestRunner(
                stream=captured,
                verbosity=2,
            ).run(
                unittest.TestSuite(
                    (
                        DeliberatePresenceFailure(),
                        DeliberateReprFailure(),
                    )
                )
            )
        finally:
            logger.removeHandler(handler)

        output = captured.getvalue()
        self.assertEqual(len(result.failures), 2)
        for forbidden in (
            FAKE_PASSWORD,
            "fake_user",
            "example.invalid",
            "fake_db",
            FAKE_LLM_KEY,
            FAKE_ADMIN_SECRET,
            FAKE_A_TOKEN,
            FAKE_B_TOKEN,
        ):
            self.assertNotIn(forbidden, output)


if __name__ == "__main__":
    unittest.main()
