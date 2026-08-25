import os
import threading
import unittest
import uuid

from db import CaseStateError, Database, StatementAlreadySubmitted
from validation import build_statement_content


def valid_statement(role, run_id):
    return build_statement_content(
        role,
        {
            "start": f"事情从一次共同安排的讨论开始，测试编号 {run_id}。",
            "timeline": "",
            "complaint": "对方临时改变安排，而且没有提前说明。",
            "own": "我提高了声音，并且打断了对方说话。",
            "emotion": "",
            "need": "提前告知",
            "request": "希望以后改变共同安排前先沟通。",
            "self_reflect": "",
            "evidence": "",
        },
    )


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL"),
    "POSTGRES_REAL_TEST = NOT RUN (TEST_DATABASE_URL is not set)",
)
class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.first_session = Database(
            os.environ["TEST_DATABASE_URL"], min_size=1, max_size=2
        )
        cls.second_session = Database(
            os.environ["TEST_DATABASE_URL"], min_size=1, max_size=2
        )
        cls.first_session.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.first_session.close()
        cls.second_session.close()

    def _create_map_ready_case(self, prefix):
        run_id = uuid.uuid4().hex
        case_id, a_token, b_token = self.first_session.create_case(
            f"{prefix}_{run_id}"
        )
        try:
            for database, role, empty_content in (
                (self.first_session, "A", " \n "),
                (self.second_session, "B", ""),
            ):
                with self.assertRaises(ValueError):
                    database.save_statement(case_id, role, empty_content)
            self.assertEqual(
                self.first_session.get_submission_status(case_id),
                {"A": False, "B": False},
            )
            self.assertEqual(
                self.first_session.get_case(case_id)["status"],
                "COLLECTING",
            )

            self.first_session.save_statement(
                case_id,
                "A",
                valid_statement("A", run_id),
            )
            self.second_session.save_statement(
                case_id,
                "B",
                valid_statement("B", run_id),
            )
            reservation = self.first_session.claim_artifact(
                case_id, "DISPUTE_MAP"
            )
            self.assertIsNotNone(reservation)
            self.first_session.complete_artifact(
                case_id, reservation, "DISPUTE_MAP", f"MAP_{run_id}"
            )
        except BaseException:
            self._cleanup_and_assert(case_id)
            raise
        return run_id, case_id, a_token, b_token

    def _cleanup_and_assert(self, case_id):
        with self.first_session._connection() as connection:
            connection.execute("DELETE FROM cases WHERE case_id = %s", (case_id,))
            residual = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM cases WHERE case_id = %s) AS cases,
                    (SELECT COUNT(*) FROM statements WHERE case_id = %s)
                        AS statements,
                    (SELECT COUNT(*) FROM messages WHERE case_id = %s)
                        AS messages,
                    (SELECT COUNT(*) FROM artifacts WHERE case_id = %s)
                        AS artifacts
                """,
                (case_id, case_id, case_id, case_id),
            ).fetchone()
        self.assertEqual(
            residual,
            {"cases": 0, "statements": 0, "messages": 0, "artifacts": 0},
        )

    def test_shared_case_and_evidence_freeze_flow(self):
        database_a = self.first_session
        database_b = self.second_session
        run_id, case_id, a_token, b_token = self._create_map_ready_case(
            "EVIDENCE_FREEZE_INTEGRATION"
        )
        try:
            self.assertEqual(database_a.authenticate(case_id, a_token), "A")
            self.assertEqual(database_b.authenticate(case_id, b_token), "B")
            self.assertIsNone(database_b.authenticate(case_id, "wrong-token"))
            with self.assertRaises(StatementAlreadySubmitted):
                database_a.save_statement(case_id, "A", "duplicate")

            database_a.add_message(case_id, "A", f"PG_CHAT_A_{run_id}")
            database_b.add_message(case_id, "B", f"PG_CHAT_B_{run_id}")
            database_a.ensure_judge_intervention_allowed(case_id)
            database_a.add_message(case_id, "JUDGE", f"PG_JUDGE_{run_id}")
            self.assertTrue(database_b.pause_case(case_id, "B"))
            self.assertFalse(database_a.resume_case(case_id, "A"))
            self.assertTrue(database_b.resume_case(case_id, "B"))

            request = database_a.request_arbitration(case_id, "A")
            self.assertEqual(request["status"], "ARBITRATION_PENDING")
            self.assertEqual(request["arbitration_requested_by"], "A")
            database_a.add_message(case_id, "A", f"PENDING_MSG_A_{run_id}")
            database_b.add_message(case_id, "B", f"PENDING_MSG_B_{run_id}")
            database_b.ensure_judge_intervention_allowed(case_id)
            self.assertFalse(database_a.pause_case(case_id, "A"))

            evidence = database_b.confirm_arbitration(case_id, "B")
            snapshot = evidence["snapshot"]
            snapshot_hash = evidence["evidence_hash"]
            self.assertEqual(database_a.get_case(case_id)["status"], "ARBITRATING")
            self.assertEqual(snapshot["requester"], "A")
            self.assertEqual(snapshot["confirmer"], "B")
            self.assertIn(f"PENDING_MSG_A_{run_id}", evidence["content"])
            self.assertIn(f"PENDING_MSG_B_{run_id}", evidence["content"])
            self.assertIn(f"PG_JUDGE_{run_id}", evidence["content"])
            self.assertEqual(
                snapshot["message_cutoff_id"], snapshot["messages"][-1]["id"]
            )
            self.assertEqual(
                database_a.get_arbitration_evidence(case_id)["evidence_hash"],
                snapshot_hash,
            )

            before_rejected_write = len(database_a.get_messages(case_id))
            for sender in ("A", "B", "JUDGE"):
                with self.subTest(sender=sender):
                    with self.assertRaises(CaseStateError):
                        database_a.add_message(
                            case_id,
                            sender,
                            f"THIS_MUST_NOT_ENTER_{sender}_{run_id}",
                        )
            self.assertEqual(len(database_a.get_messages(case_id)), before_rejected_write)
            with self.assertRaises(CaseStateError):
                database_a.ensure_judge_intervention_allowed(case_id)
            self.assertFalse(database_a.pause_case(case_id, "A"))
            self.assertFalse(database_a.resume_case(case_id, "A"))

            with database_a._connection() as connection:
                stored = connection.execute(
                    """
                    SELECT a_token_hash, b_token_hash
                    FROM cases WHERE case_id = %s
                    """,
                    (case_id,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO messages(case_id, sender, content, created_at)
                    VALUES (%s, 'SYSTEM', %s, NOW())
                    """,
                    (case_id, f"AFTER_CUTOFF_DIRECT_{run_id}"),
                )
            frozen_again = database_a.get_arbitration_evidence(case_id)
            self.assertEqual(frozen_again["evidence_hash"], snapshot_hash)
            self.assertNotIn(f"AFTER_CUTOFF_DIRECT_{run_id}", frozen_again["content"])
            for forbidden in (
                a_token,
                b_token,
                stored["a_token_hash"],
                stored["b_token_hash"],
                "DATABASE_URL",
                "LLM_API_KEY",
                "ADMIN_CREATE_SECRET",
                "Authorization",
            ):
                self.assertNotIn(forbidden, evidence["content"])

            final_reservation = database_a.claim_artifact(
                case_id, "FINAL_JUDGMENT"
            )
            self.assertIsNotNone(final_reservation)
            normal_id = database_a.save_checkpoint(
                case_id,
                "JUDGMENT_NORMAL",
                "normal checkpoint",
                snapshot_hash,
            )
            self.assertEqual(
                database_b.save_checkpoint(
                    case_id,
                    "JUDGMENT_NORMAL",
                    "normal checkpoint",
                    snapshot_hash,
                ),
                normal_id,
            )
            database_a.save_checkpoint(
                case_id,
                "JUDGMENT_SWAPPED",
                "swapped checkpoint",
                snapshot_hash,
            )
            database_a.save_checkpoint(
                case_id,
                "META_JUDGMENT",
                "meta checkpoint",
                snapshot_hash,
            )
            database_a.complete_artifact(
                case_id,
                final_reservation,
                "FINAL_JUDGMENT",
                "meta checkpoint",
            )
            self.assertEqual(database_b.get_case(case_id)["status"], "CLOSED")
            self.assertEqual(
                database_b.get_artifact(case_id, "FINAL_JUDGMENT")[
                    "evidence_hash"
                ],
                snapshot_hash,
            )
            with self.assertRaises(CaseStateError):
                database_b.add_message(case_id, "B", "closed write")
        finally:
            self._cleanup_and_assert(case_id)

    def test_message_and_freeze_lock_race_has_no_ghost_message(self):
        run_id, case_id, _a_token, _b_token = self._create_map_ready_case(
            "EVIDENCE_LOCK_RACE"
        )
        marker = f"LAST_MESSAGE_{run_id}"
        barrier = threading.Barrier(2)
        outcome = {}
        self.first_session.request_arbitration(case_id, "A")

        def send_message():
            barrier.wait()
            try:
                self.first_session.add_message(case_id, "A", marker)
            except CaseStateError:
                outcome["message"] = "rejected"
            else:
                outcome["message"] = "inserted"

        def freeze_evidence():
            barrier.wait()
            outcome["evidence"] = self.second_session.confirm_arbitration(
                case_id, "B"
            )

        sender = threading.Thread(target=send_message)
        freezer = threading.Thread(target=freeze_evidence)
        try:
            sender.start()
            freezer.start()
            sender.join(timeout=30)
            freezer.join(timeout=30)
            self.assertFalse(sender.is_alive())
            self.assertFalse(freezer.is_alive())
            content = outcome["evidence"]["content"]
            if outcome["message"] == "inserted":
                self.assertIn(marker, content)
            else:
                self.assertNotIn(marker, content)
            inserted = any(
                message["content"] == marker
                for message in self.first_session.get_messages(case_id)
            )
            self.assertEqual(inserted, marker in content)
        finally:
            self._cleanup_and_assert(case_id)

    def test_concurrent_requests_keep_one_requester_and_one_snapshot(self):
        _run_id, case_id, _a_token, _b_token = self._create_map_ready_case(
            "EVIDENCE_REQUEST_RACE"
        )
        barrier = threading.Barrier(2)
        results = {}

        def request(database, role):
            barrier.wait()
            results[role] = database.request_arbitration(case_id, role)

        thread_a = threading.Thread(target=request, args=(self.first_session, "A"))
        thread_b = threading.Thread(target=request, args=(self.second_session, "B"))
        try:
            thread_a.start()
            thread_b.start()
            thread_a.join(timeout=30)
            thread_b.join(timeout=30)
            self.assertFalse(thread_a.is_alive())
            self.assertFalse(thread_b.is_alive())
            requester = self.first_session.get_case(case_id)[
                "arbitration_requested_by"
            ]
            self.assertIn(requester, {"A", "B"})
            self.assertEqual(results["A"]["arbitration_requested_by"], requester)
            self.assertEqual(results["B"]["arbitration_requested_by"], requester)
            confirmer = "B" if requester == "A" else "A"
            evidence = self.first_session.confirm_arbitration(case_id, confirmer)
            idempotent = self.second_session.confirm_arbitration(case_id, confirmer)
            self.assertEqual(evidence["id"], idempotent["id"])
            with self.first_session._connection() as connection:
                count = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM artifacts
                    WHERE case_id = %s AND kind = 'ARBITRATION_EVIDENCE'
                    """,
                    (case_id,),
                ).fetchone()["count"]
            self.assertEqual(count, 1)
        finally:
            self._cleanup_and_assert(case_id)


if __name__ == "__main__":
    unittest.main()
