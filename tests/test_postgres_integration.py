import threading
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

import database_resources
from db import (
    CaseStateError,
    Database,
    DatabaseError,
    StatementAlreadySubmitted,
)
from dev_tools import delete_dev_case, get_dev_state, seed_dev_case
from integration_config import load_test_database_url
from validation import build_statement_content


DEV_SETTINGS = SimpleNamespace(dev_mode=True)
TEST_DATABASE_URL, _TEST_DATABASE_SOURCE = load_test_database_url()


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
    TEST_DATABASE_URL,
    "POSTGRES_REAL_TEST = NOT RUN (TEST_DATABASE_URL is not set)",
)
class PostgreSQLIntegrationTests(unittest.TestCase):
    gate_run_prefix = f"INTERACTION_GATE_{uuid.uuid4().hex}"
    gate_case_ids = set()

    @classmethod
    def setUpClass(cls):
        cls.first_session = Database(
            TEST_DATABASE_URL, min_size=1, max_size=2
        )
        cls.second_session = Database(
            TEST_DATABASE_URL, min_size=1, max_size=2
        )
        cls.first_session.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.first_session.close()
        cls.second_session.close()

    def _create_gate_case(self, label):
        run_id = uuid.uuid4().hex
        case_id, a_token, b_token = self.first_session.create_case(
            f"{self.gate_run_prefix}_{label}_{run_id}"
        )
        type(self).gate_case_ids.add(case_id)
        return run_id, case_id, a_token, b_token

    def _create_map_ready_case(self, prefix):
        run_id, case_id, a_token, b_token = self._create_gate_case(
            prefix
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
                        AS artifacts,
                    (SELECT COUNT(*) FROM case_notifications WHERE case_id = %s)
                        AS notifications
                """,
                (case_id, case_id, case_id, case_id, case_id),
            ).fetchone()
        self.assertEqual(
            residual,
            {
                "cases": 0,
                "statements": 0,
                "messages": 0,
                "artifacts": 0,
                "notifications": 0,
            },
        )

    def test_schema_initialization_has_required_tables(self):
        required_tables = {
            "cases",
            "statements",
            "messages",
            "artifacts",
            "case_notifications",
        }
        with self.first_session._connection() as connection:
            rows = connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = ANY(%s)
                """,
                (list(required_tables),),
            ).fetchall()
            notification_columns = connection.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'case_notifications'
                """
            ).fetchall()
        self.assertEqual({row["table_name"] for row in rows}, required_tables)
        self.assertTrue(
            {
                "id",
                "case_id",
                "recipient_role",
                "event_type",
                "actor_role",
                "created_at",
                "read_at",
            }.issubset({row["column_name"] for row in notification_columns})
        )

    def test_cached_pool_supports_fresh_current_wrappers(self):
        database_resources.get_postgres_pool.clear()
        database_a = database_resources.get_database(TEST_DATABASE_URL)
        database_b = database_resources.get_database(TEST_DATABASE_URL)
        self.assertIsNot(database_a, database_b)
        self.assertIs(database_a.pool, database_b.pool)
        self.assertTrue(database_a.health_check())
        self.assertTrue(hasattr(database_b, "get_arbitration_evidence"))
        self.assertTrue(hasattr(database_b, "get_unread_notifications"))

        run_id = uuid.uuid4().hex
        case_id = None
        try:
            case_id, _a_token, _b_token = database_a.create_case(
                f"{self.gate_run_prefix}_RESOURCE_LIFECYCLE_{run_id}"
            )
            type(self).gate_case_ids.add(case_id)
            self.assertEqual(
                database_b.get_case(case_id)["status"],
                "COLLECTING",
            )
            database_a.save_statement(
                case_id,
                "A",
                valid_statement("A", run_id),
            )
            database_b.save_statement(
                case_id,
                "B",
                valid_statement("B", run_id),
            )
            reservation = database_a.claim_artifact(case_id, "DISPUTE_MAP")
            self.assertIsNotNone(reservation)
            database_a.complete_artifact(
                case_id,
                reservation,
                "DISPUTE_MAP",
                f"RESOURCE_MAP_{run_id}",
            )
            marker = f"RESOURCE_MESSAGE_{run_id}"
            database_a.add_message(case_id, "A", marker)
            self.assertEqual(
                database_b.get_messages(case_id)[-1]["content"],
                marker,
            )

            database_a.request_arbitration(case_id, "A")
            evidence = database_b.confirm_arbitration(case_id, "B")
            self.assertEqual(
                database_a.get_arbitration_evidence(case_id)["evidence_hash"],
                evidence["evidence_hash"],
            )
            notifications = database_b.get_unread_notifications(case_id, "A")
            self.assertEqual(len(notifications), 1)
            self.assertEqual(
                notifications[0]["event_type"],
                "ARBITRATION_ACCEPTED",
            )

            database_a.close()
            self.assertTrue(database_b.health_check())
        finally:
            if case_id is not None:
                self._cleanup_and_assert(case_id)

    def test_developer_scenarios_use_real_postgres_and_cleanup(self):
        scenarios = (
            "MEDIATING",
            "ARBITRATION_PENDING_A",
            "ARBITRATING",
            "CLOSED",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                dev_case = seed_dev_case(
                    DEV_SETTINGS,
                    self.first_session,
                    "weekend_plan",
                    scenario,
                )
                type(self).gate_case_ids.add(dev_case.case_id)
                try:
                    state = get_dev_state(
                        DEV_SETTINGS,
                        self.second_session,
                        dev_case.case_id,
                    )
                    self.assertTrue(state["a_submitted"])
                    self.assertTrue(state["b_submitted"])
                    self.assertTrue(state["dispute_map"])
                    self.assertGreaterEqual(state["message_count"], 1)

                    if scenario == "MEDIATING":
                        self.assertEqual(state["status"], "MEDIATING")
                        self.assertFalse(state["evidence"])
                        self.assertFalse(state["final"])
                    elif scenario == "ARBITRATION_PENDING_A":
                        self.assertEqual(
                            state["status"],
                            "ARBITRATION_PENDING",
                        )
                        self.assertEqual(state["arbitration_request"], "A")
                        self.assertFalse(state["evidence"])
                        self.second_session.add_message(
                            dev_case.case_id,
                            "B",
                            "开发集成测试：待确认阶段消息仍可写。",
                        )
                    elif scenario == "ARBITRATING":
                        self.assertEqual(state["status"], "ARBITRATING")
                        self.assertTrue(state["evidence"])
                        self.assertTrue(state["evidence_hash_preview"])
                        with self.assertRaises(CaseStateError):
                            self.second_session.add_message(
                                dev_case.case_id,
                                "A",
                                "开发集成测试：冻结后必须拒绝。",
                            )
                    elif scenario == "CLOSED":
                        self.assertEqual(state["status"], "CLOSED")
                        self.assertTrue(state["evidence"])
                        self.assertTrue(state["judgment_normal"])
                        self.assertTrue(state["judgment_swapped"])
                        self.assertTrue(state["meta"])
                        self.assertTrue(state["final"])
                finally:
                    delete_dev_case(
                        DEV_SETTINGS,
                        self.first_session,
                        dev_case.case_id,
                    )
                    self._cleanup_and_assert(dev_case.case_id)

    def test_message_return_tracks_case_predecessor_across_global_id_gap(self):
        run_id, case_id, _a_token, _b_token = self._create_map_ready_case(
            "MESSAGE_PREDECESSOR"
        )
        _gap_run_id, gap_case_id, _gap_a_token, _gap_b_token = (
            self._create_map_ready_case("MESSAGE_GLOBAL_GAP")
        )
        try:
            first = self.first_session.add_message(
                case_id,
                "A",
                f"FIRST_{run_id}",
            )
            gap = self.first_session.add_message(
                gap_case_id,
                "A",
                f"GLOBAL_GAP_{run_id}",
            )
            second = self.second_session.add_message(
                case_id,
                "B",
                f"SECOND_{run_id}",
            )
            third = self.first_session.add_message(
                case_id,
                "A",
                f"THIRD_{run_id}",
            )

            expected_keys = {
                "id",
                "case_id",
                "sender",
                "content",
                "created_at",
                "case_status",
                "case_updated_at",
                "previous_message_id",
            }
            self.assertEqual(set(first), expected_keys)
            self.assertEqual(first["previous_message_id"], 0)
            self.assertEqual(first["case_status"], "MEDIATING")
            self.assertLess(first["id"], gap["id"])
            self.assertLess(gap["id"], second["id"])
            self.assertNotEqual(second["id"], first["id"] + 1)
            self.assertEqual(second["previous_message_id"], first["id"])
            self.assertEqual(third["previous_message_id"], second["id"])
            self.assertEqual(
                [message["id"] for message in self.first_session.get_messages(case_id)],
                [first["id"], second["id"], third["id"]],
            )
        finally:
            self._cleanup_and_assert(case_id)
            self._cleanup_and_assert(gap_case_id)

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
            database_b.cancel_arbitration_request(case_id, "B")
            persistence_reader = Database(
                TEST_DATABASE_URL,
                min_size=1,
                max_size=1,
            )
            try:
                declined = persistence_reader.get_unread_notifications(
                    case_id,
                    "A",
                )
            finally:
                persistence_reader.close()
            self.assertEqual(len(declined), 1)
            self.assertEqual(
                declined[0]["event_type"],
                "ARBITRATION_DECLINED",
            )
            self.assertEqual(database_b.get_unread_notifications(case_id, "B"), [])
            self.assertTrue(
                database_a.mark_notification_read(
                    case_id,
                    declined[0]["id"],
                    "A",
                )
            )
            self.assertEqual(
                database_a.get_unread_notifications(case_id, "A"),
                [],
            )
            with database_a._connection() as connection:
                declined_read_at = connection.execute(
                    """
                    SELECT read_at
                    FROM case_notifications
                    WHERE id = %s AND case_id = %s
                    """,
                    (declined[0]["id"], case_id),
                ).fetchone()["read_at"]
            self.assertIsNotNone(declined_read_at)
            request = database_a.request_arbitration(case_id, "A")
            self.assertEqual(request["status"], "ARBITRATION_PENDING")
            database_a.add_message(case_id, "A", f"PENDING_MSG_A_{run_id}")
            database_b.add_message(case_id, "B", f"PENDING_MSG_B_{run_id}")
            database_b.ensure_judge_intervention_allowed(case_id)
            self.assertFalse(database_a.pause_case(case_id, "A"))

            evidence = database_b.confirm_arbitration(case_id, "B")
            persistence_reader = Database(
                TEST_DATABASE_URL,
                min_size=1,
                max_size=1,
            )
            try:
                accepted = persistence_reader.get_unread_notifications(
                    case_id,
                    "A",
                )
            finally:
                persistence_reader.close()
            self.assertEqual(len(accepted), 1)
            self.assertEqual(
                accepted[0]["event_type"],
                "ARBITRATION_ACCEPTED",
            )
            self.assertEqual(accepted[0]["actor_role"], "B")
            self.assertEqual(accepted[0]["recipient_role"], "A")
            self.assertEqual(database_b.get_unread_notifications(case_id, "B"), [])
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

    def test_notification_failure_rolls_back_decline_and_accept(self):
        _run_id, case_id, _a_token, _b_token = self._create_map_ready_case(
            "NOTIFICATION_ROLLBACK"
        )
        try:
            self.first_session.add_message(case_id, "A", "Rollback marker")
            self.first_session.request_arbitration(case_id, "A")

            with patch.object(
                Database,
                "_insert_notification",
                side_effect=DatabaseError("injected notification failure"),
            ):
                with self.assertRaises(DatabaseError):
                    self.second_session.cancel_arbitration_request(case_id, "B")

            after_decline_failure = self.first_session.get_case(case_id)
            self.assertEqual(
                after_decline_failure["status"],
                "ARBITRATION_PENDING",
            )
            self.assertEqual(
                after_decline_failure["arbitration_requested_by"],
                "A",
            )
            self.assertEqual(
                self.first_session.get_unread_notifications(case_id, "A"),
                [],
            )

            with patch.object(
                Database,
                "_insert_notification",
                side_effect=DatabaseError("injected notification failure"),
            ):
                with self.assertRaises(DatabaseError):
                    self.second_session.confirm_arbitration(case_id, "B")

            after_accept_failure = self.first_session.get_case(case_id)
            self.assertEqual(
                after_accept_failure["status"],
                "ARBITRATION_PENDING",
            )
            self.assertEqual(
                after_accept_failure["arbitration_requested_by"],
                "A",
            )
            self.assertIsNone(
                self.first_session.get_arbitration_evidence(case_id)
            )
            self.assertEqual(
                self.first_session.get_unread_notifications(case_id, "A"),
                [],
            )
        finally:
            self._cleanup_and_assert(case_id)

    def test_auto_map_flow_starts_only_after_second_statement(self):
        run_id, case_id, _a_token, _b_token = self._create_gate_case(
            "AUTO_MAP"
        )
        generation = Mock(return_value=f"MAP_{run_id}")
        try:
            self.first_session.save_statement(
                case_id,
                "A",
                valid_statement("A", run_id),
            )
            self.assertEqual(generation.call_count, 0)
            self.assertIsNone(
                self.first_session.get_artifact(case_id, "DISPUTE_MAP")
            )
            self.assertEqual(
                self.first_session.get_case(case_id)["status"],
                "COLLECTING",
            )

            self.second_session.save_statement(
                case_id,
                "B",
                valid_statement("B", run_id),
            )
            self.assertEqual(
                self.first_session.get_case(case_id)["status"],
                "READY_FOR_MAP",
            )
            reservation = self.first_session.claim_artifact(
                case_id,
                "DISPUTE_MAP",
            )
            self.assertIsNotNone(reservation)
            self.first_session.complete_artifact(
                case_id,
                reservation,
                "DISPUTE_MAP",
                generation(),
            )
            self.assertEqual(generation.call_count, 1)
            self.assertEqual(
                self.second_session.get_case(case_id)["status"],
                "MAP_READY",
            )
        finally:
            self._cleanup_and_assert(case_id)

    def test_concurrent_dispute_map_claim_and_failed_retry_have_one_winner(self):
        run_id, case_id, _a_token, _b_token = self._create_gate_case(
            "MAP_CLAIM"
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
        barrier = threading.Barrier(2)
        claims = []

        def claim(database):
            barrier.wait()
            claims.append(database.claim_artifact(case_id, "DISPUTE_MAP"))

        first = threading.Thread(target=claim, args=(self.first_session,))
        second = threading.Thread(target=claim, args=(self.second_session,))
        try:
            first.start()
            second.start()
            first.join(timeout=30)
            second.join(timeout=30)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            winners = [claim_id for claim_id in claims if claim_id is not None]
            self.assertEqual(len(winners), 1)
            reservation = winners[0]

            with self.first_session._connection() as connection:
                artifact_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM artifacts
                    WHERE case_id = %s AND kind = 'DISPUTE_MAP'
                    """,
                    (case_id,),
                ).fetchone()["count"]
            self.assertEqual(artifact_count, 1)

            self.assertTrue(
                self.first_session.fail_artifact(
                    case_id,
                    reservation,
                    "DISPUTE_MAP",
                )
            )
            failed_artifact = self.second_session.get_artifact(
                case_id,
                "DISPUTE_MAP",
            )
            self.assertEqual(failed_artifact["content"], "")
            self.assertIsNotNone(failed_artifact["generation_failed_at"])
            self.assertEqual(
                self.first_session.get_case(case_id)["status"],
                "READY_FOR_MAP",
            )
            self.assertIsNotNone(
                self.first_session.get_statement(case_id, "A")
            )
            self.assertIsNotNone(
                self.second_session.get_statement(case_id, "B")
            )
            retry_barrier = threading.Barrier(2)
            retries = []

            def retry(database):
                retry_barrier.wait()
                retries.append(
                    database.retry_failed_artifact(case_id, "DISPUTE_MAP")
                )

            first_retry = threading.Thread(
                target=retry,
                args=(self.first_session,),
            )
            second_retry = threading.Thread(
                target=retry,
                args=(self.second_session,),
            )
            first_retry.start()
            second_retry.start()
            first_retry.join(timeout=30)
            second_retry.join(timeout=30)
            self.assertEqual(
                [retry_id for retry_id in retries if retry_id is not None],
                [reservation],
            )
            generation = Mock(return_value=f"MAP_{run_id}")
            self.first_session.complete_artifact(
                case_id,
                reservation,
                "DISPUTE_MAP",
                generation(),
            )
            self.assertEqual(generation.call_count, 1)
            self.assertEqual(
                self.second_session.get_case(case_id)["status"],
                "MAP_READY",
            )
            with self.first_session._connection() as connection:
                final_artifact_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM artifacts
                    WHERE case_id = %s AND kind = 'DISPUTE_MAP'
                    """,
                    (case_id,),
                ).fetchone()["count"]
            self.assertEqual(final_artifact_count, 1)
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
                notification_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM case_notifications
                    WHERE case_id = %s AND event_type = 'ARBITRATION_ACCEPTED'
                    """,
                    (case_id,),
                ).fetchone()["count"]
            self.assertEqual(count, 1)
            self.assertEqual(notification_count, 1)
        finally:
            self._cleanup_and_assert(case_id)

    def test_zz_gate_cases_have_zero_residual(self):
        case_ids = list(type(self).gate_case_ids)
        self.assertTrue(case_ids)
        with self.first_session._connection() as connection:
            residual = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM cases WHERE case_id = ANY(%s)) AS cases,
                    (SELECT COUNT(*) FROM statements WHERE case_id = ANY(%s))
                        AS statements,
                    (SELECT COUNT(*) FROM messages WHERE case_id = ANY(%s))
                        AS messages,
                    (SELECT COUNT(*) FROM artifacts WHERE case_id = ANY(%s))
                        AS artifacts,
                    (SELECT COUNT(*) FROM case_notifications
                     WHERE case_id = ANY(%s)) AS notifications
                """,
                (case_ids, case_ids, case_ids, case_ids, case_ids),
            ).fetchone()
        self.assertEqual(
            residual,
            {
                "cases": 0,
                "statements": 0,
                "messages": 0,
                "artifacts": 0,
                "notifications": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
