import hashlib
import unittest
from unittest.mock import patch

import db


class DatabaseSecurityTests(unittest.TestCase):
    def test_private_tokens_have_role_prefix_and_high_entropy(self):
        first = db._private_token("A")
        second = db._private_token("A")

        self.assertTrue(first.startswith("A-"))
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 34)

    def test_token_hash_is_sha256_and_does_not_contain_plaintext(self):
        token = "A-test-token-value"
        token_hash = db.hash_token(token)

        self.assertEqual(token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, token_hash)

    def test_schema_contains_hash_columns_not_plain_token_columns(self):
        schema = "\n".join(db.SCHEMA_STATEMENTS).lower()

        self.assertIn("a_token_hash", schema)
        self.assertIn("b_token_hash", schema)
        self.assertNotIn("a_token text", schema)
        self.assertNotIn("b_token text", schema)
        self.assertIn("unique(case_id, role)", schema)
        self.assertIn("unique(case_id, kind)", schema)
        for kind in (
            "judgment_normal",
            "judgment_swapped",
            "meta_judgment",
        ):
            self.assertIn(kind, schema)

    def test_final_artifact_and_checkpoints_have_unique_kinds(self):
        self.assertIn("FINAL_JUDGMENT", db.PUBLIC_ARTIFACT_KINDS)
        self.assertEqual(
            db.CHECKPOINT_ARTIFACT_KINDS,
            {"JUDGMENT_NORMAL", "JUDGMENT_SWAPPED", "META_JUDGMENT"},
        )

    @patch("db.ConnectionPool")
    def test_pool_separates_acquire_and_connect_timeouts(self, pool_class):
        database = db.Database("postgresql://test.invalid/database")

        _, kwargs = pool_class.call_args
        self.assertEqual(kwargs["min_size"], 2)
        self.assertEqual(kwargs["max_size"], 5)
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual(kwargs["max_idle"], 60)
        self.assertEqual(kwargs["max_lifetime"], 1800)
        self.assertEqual(kwargs["reconnect_timeout"], 30)
        self.assertIs(kwargs["check"], pool_class.check_connection)
        self.assertTrue(kwargs["kwargs"]["autocommit"])
        self.assertEqual(kwargs["kwargs"]["connect_timeout"], 20)
        self.assertEqual(kwargs["kwargs"]["sslmode"], "require")
        self.assertEqual(kwargs["kwargs"]["keepalives"], 1)
        self.assertEqual(kwargs["kwargs"]["keepalives_idle"], 30)
        self.assertEqual(kwargs["kwargs"]["keepalives_interval"], 10)
        self.assertEqual(kwargs["kwargs"]["keepalives_count"], 3)
        pool_class.return_value.open.assert_called_once_with(
            wait=True,
            timeout=30,
        )
        database.close()


if __name__ == "__main__":
    unittest.main()
