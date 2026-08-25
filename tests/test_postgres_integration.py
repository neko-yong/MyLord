import os
import unittest
import uuid

from db import Database, StatementAlreadySubmitted


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL"),
    "POSTGRES_REAL_TEST = NOT RUN (TEST_DATABASE_URL is not set)",
)
class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.first_session = Database(
            os.environ["TEST_DATABASE_URL"],
            min_size=1,
            max_size=2,
        )
        cls.second_session = Database(
            os.environ["TEST_DATABASE_URL"],
            min_size=1,
            max_size=2,
        )
        cls.first_session.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.first_session.close()
        cls.second_session.close()

    def test_shared_case_flow(self):
        database_a = self.first_session
        database_b = self.second_session
        run_id = uuid.uuid4().hex
        case_id, a_token, b_token = database_a.create_case(
            f"E2E_TEST_INTEGRATION_{run_id}"
        )

        try:
            self.assertEqual(database_a.authenticate(case_id, a_token), "A")
            self.assertEqual(database_b.authenticate(case_id, b_token), "B")
            self.assertIsNone(database_b.authenticate(case_id, "wrong-token"))

            database_a.save_statement(
                case_id,
                "A",
                f"PG_TEST_A_{run_id}",
            )
            overview_for_b = database_b.get_case_overview(case_id)
            self.assertEqual(
                overview_for_b["submitted"],
                {"A": True, "B": False},
            )

            with self.assertRaises(StatementAlreadySubmitted):
                database_a.save_statement(case_id, "A", "duplicate")

            database_b.save_statement(
                case_id,
                "B",
                f"PG_TEST_B_{run_id}",
            )
            self.assertTrue(database_a.both_submitted(case_id))
            self.assertEqual(database_a.get_case(case_id)["status"], "READY_FOR_MAP")

            map_reservation = database_a.claim_artifact(case_id, "DISPUTE_MAP")
            self.assertIsNotNone(map_reservation)
            self.assertIsNone(database_b.claim_artifact(case_id, "DISPUTE_MAP"))
            database_a.complete_artifact(
                case_id,
                map_reservation,
                "DISPUTE_MAP",
                "shared dispute map",
            )
            self.assertEqual(
                database_b.get_artifact(case_id, "DISPUTE_MAP")["content"],
                "shared dispute map",
            )

            database_a.add_message(case_id, "A", f"PG_CHAT_A_{run_id}")
            database_b.add_message(case_id, "B", f"PG_CHAT_B_{run_id}")
            self.assertEqual(len(database_a.get_messages(case_id)), 2)
            self.assertEqual(len(database_b.get_messages(case_id)), 2)

            snapshot = database_a.get_mediation_snapshot(case_id)
            self.assertEqual(snapshot["case"]["status"], "MEDIATING")
            self.assertEqual(snapshot["artifact"]["content"], "shared dispute map")
            self.assertEqual(len(snapshot["messages"]), 2)

            last_message_id = snapshot["messages"][-1]["id"]
            database_a.add_message(
                case_id,
                "A",
                f"PG_CHAT_INCREMENTAL_{run_id}",
            )
            incremental = database_b.get_mediation_snapshot(
                case_id,
                last_message_id,
            )
            self.assertEqual(len(incremental["messages"]), 1)

            self.assertTrue(database_b.pause_case(case_id, "B"))
            self.assertEqual(database_a.get_case(case_id)["paused_by"], "B")
            self.assertFalse(database_a.resume_case(case_id, "A"))
            self.assertTrue(database_b.resume_case(case_id, "B"))
            self.assertEqual(database_a.get_case(case_id)["status"], "MEDIATING")

            with database_a._connection() as connection:
                stored = connection.execute(
                    """
                    SELECT a_token_hash, b_token_hash
                    FROM cases
                    WHERE case_id = %s
                    """,
                    (case_id,),
                ).fetchone()
            self.assertNotEqual(stored["a_token_hash"], a_token)
            self.assertNotEqual(stored["b_token_hash"], b_token)

            final_reservation = database_a.claim_artifact(
                case_id,
                "FINAL_JUDGMENT",
            )
            self.assertIsNotNone(final_reservation)
            normal_id = database_a.save_checkpoint(
                case_id,
                "JUDGMENT_NORMAL",
                "normal checkpoint",
            )
            self.assertEqual(
                database_b.save_checkpoint(
                    case_id,
                    "JUDGMENT_NORMAL",
                    "normal checkpoint",
                ),
                normal_id,
            )
            database_a.save_checkpoint(
                case_id,
                "JUDGMENT_SWAPPED",
                "swapped checkpoint",
            )
            database_a.save_checkpoint(
                case_id,
                "META_JUDGMENT",
                "meta checkpoint",
            )
            database_a.complete_artifact(
                case_id,
                final_reservation,
                "FINAL_JUDGMENT",
                "meta checkpoint",
            )
            database_b.complete_artifact(
                case_id,
                final_reservation,
                "FINAL_JUDGMENT",
                "meta checkpoint",
            )
            self.assertEqual(
                database_b.get_artifact(case_id, "FINAL_JUDGMENT")["content"],
                "meta checkpoint",
            )
            self.assertIsNone(
                database_b.claim_artifact(case_id, "FINAL_JUDGMENT")
            )
        finally:
            with database_a._connection() as connection:
                connection.execute(
                    "DELETE FROM cases WHERE case_id = %s",
                    (case_id,),
                )


if __name__ == "__main__":
    unittest.main()
