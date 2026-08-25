import unittest
from types import SimpleNamespace

from arbitration import run_final_arbitration
from db import CaseStateError, DatabaseUnavailable
from dev_fixtures import get_fixture
from dev_memory_db import (
    DEV_TITLE_PREFIX,
    DevMemoryDatabase,
    new_dev_local_store,
)
from dev_tools import (
    FinalCompleteFailureDatabase,
    delete_dev_case,
    seed_dev_case,
)
from llm import LLMError
from mock_llm import MockLLM


LOCAL_SETTINGS = SimpleNamespace(dev_mode=True, dev_database_mode="local")
POSTGRES_SETTINGS = SimpleNamespace(dev_mode=True, dev_database_mode="postgres")
PROD_SETTINGS = SimpleNamespace(dev_mode=False, dev_database_mode="local")


class DevMemoryDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.store = new_dev_local_store(LOCAL_SETTINGS)
        self.database = DevMemoryDatabase(LOCAL_SETTINGS, self.store)

    def test_production_and_postgres_modes_cannot_construct_local_backend(self):
        with self.assertRaises(PermissionError):
            new_dev_local_store(PROD_SETTINGS)
        with self.assertRaises(PermissionError):
            DevMemoryDatabase(PROD_SETTINGS, self.store)
        with self.assertRaises(PermissionError):
            DevMemoryDatabase(
                POSTGRES_SETTINGS,
                self.store,
                database_mode="postgres",
            )

    def test_only_dev_cases_can_be_created_switched_and_deleted(self):
        with self.assertRaises(PermissionError):
            self.database.create_case("普通案件")
        dev_case = seed_dev_case(
            LOCAL_SETTINGS,
            self.database,
            "weekend_plan",
            "EMPTY",
        )
        self.assertTrue(dev_case.case_id.startswith("DEV-"))
        self.assertEqual(
            self.database.authenticate(dev_case.case_id, dev_case.a_token),
            "A",
        )
        self.assertNotIn(
            dev_case.a_token,
            repr(self.store),
        )
        self.assertFalse(
            self.database.delete_case_if_title_prefix(
                dev_case.case_id,
                "[OTHER] ",
            )
        )
        self.assertTrue(
            delete_dev_case(LOCAL_SETTINGS, self.database, dev_case.case_id)
        )
        self.assertEqual(self.store["cases"], {})

    def test_all_scenarios_seed_with_formal_local_state(self):
        expected = {
            "EMPTY": "COLLECTING",
            "A_SUBMITTED": "COLLECTING",
            "STATEMENTS_SUBMITTED": "READY_FOR_MAP",
            "MAP_READY": "MAP_READY",
            "MEDIATING": "MEDIATING",
            "PAUSED": "PAUSED",
            "ARBITRATION_PENDING_A": "ARBITRATION_PENDING",
            "ARBITRATION_PENDING_B": "ARBITRATION_PENDING",
            "ARBITRATING": "ARBITRATING",
            "CLOSED": "CLOSED",
        }
        for scenario, status in expected.items():
            with self.subTest(scenario=scenario):
                dev_case = seed_dev_case(
                    LOCAL_SETTINGS,
                    self.database,
                    "weekend_plan",
                    scenario,
                )
                self.assertEqual(
                    self.database.get_case(dev_case.case_id)["status"],
                    status,
                )
                delete_dev_case(
                    LOCAL_SETTINGS,
                    self.database,
                    dev_case.case_id,
                )

    def test_pause_dual_confirmation_evidence_and_closed_contract(self):
        dev_case = seed_dev_case(
            LOCAL_SETTINGS,
            self.database,
            "weekend_plan",
            "MEDIATING",
        )
        case_id = dev_case.case_id
        self.assertTrue(self.database.pause_case(case_id, "B"))
        self.assertFalse(self.database.resume_case(case_id, "A"))
        self.assertTrue(self.database.resume_case(case_id, "B"))

        self.database.request_arbitration(case_id, "A")
        self.database.add_message(case_id, "B", "待确认阶段仍可继续沟通。")
        self.database.cancel_arbitration_request(case_id, "A")
        self.database.request_arbitration(case_id, "A")
        evidence = self.database.confirm_arbitration(case_id, "B")
        snapshot = evidence["snapshot"]
        self.assertEqual(snapshot["requester"], "A")
        self.assertEqual(snapshot["confirmer"], "B")
        self.assertEqual(
            snapshot["message_cutoff_id"],
            snapshot["messages"][-1]["id"],
        )
        self.assertEqual(
            [item["id"] for item in snapshot["messages"]],
            sorted(item["id"] for item in snapshot["messages"]),
        )
        with self.assertRaises(CaseStateError):
            self.database.add_message(case_id, "A", "冻结后拒绝")
        self.assertFalse(self.database.pause_case(case_id, "A"))
        self.assertFalse(self.database.resume_case(case_id, "A"))

        reservation = self.database.claim_artifact(case_id, "FINAL_JUDGMENT")
        calls = {}
        mock = MockLLM(
            LOCAL_SETTINGS,
            get_fixture("weekend_plan"),
            calls,
            {"stage": "NONE", "triggered": False},
        )
        run_final_arbitration(
            database=self.database,
            ask_llm=mock,
            case_id=case_id,
            reservation_id=reservation,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(
            calls,
            {
                "JUDGMENT_NORMAL": 1,
                "JUDGMENT_SWAPPED": 1,
                "META_JUDGMENT": 1,
            },
        )
        self.assertEqual(self.database.get_case(case_id)["status"], "CLOSED")
        for kind in (
            "ARBITRATION_EVIDENCE",
            "JUDGMENT_NORMAL",
            "JUDGMENT_SWAPPED",
            "META_JUDGMENT",
            "FINAL_JUDGMENT",
        ):
            self.assertTrue(self.database.get_artifact(case_id, kind))

    def test_local_checkpoint_resume_does_not_repeat_j1(self):
        dev_case = seed_dev_case(
            LOCAL_SETTINGS,
            self.database,
            "weekend_plan",
            "ARBITRATING",
        )
        reservation = self.database.claim_artifact(
            dev_case.case_id,
            "FINAL_JUDGMENT",
        )
        calls = {}
        failure = {
            "stage": "JUDGMENT_SWAPPED",
            "triggered": False,
        }
        mock = MockLLM(
            LOCAL_SETTINGS,
            get_fixture("weekend_plan"),
            calls,
            failure,
        )
        with self.assertRaises(LLMError):
            run_final_arbitration(
                database=self.database,
                ask_llm=mock,
                case_id=dev_case.case_id,
                reservation_id=reservation,
                sleep=lambda _seconds: None,
            )
        run_final_arbitration(
            database=self.database,
            ask_llm=mock,
            case_id=dev_case.case_id,
            reservation_id=reservation,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(calls["JUDGMENT_NORMAL"], 1)
        self.assertEqual(calls["JUDGMENT_SWAPPED"], 2)
        self.assertEqual(calls["META_JUDGMENT"], 1)
        self.assertEqual(
            self.database.get_case(dev_case.case_id)["status"],
            "CLOSED",
        )

    def test_local_j1_and_meta_failure_injection_resume(self):
        expected_counts = {
            "JUDGMENT_NORMAL": {
                "JUDGMENT_NORMAL": 2,
                "JUDGMENT_SWAPPED": 1,
                "META_JUDGMENT": 1,
            },
            "META_JUDGMENT": {
                "JUDGMENT_NORMAL": 1,
                "JUDGMENT_SWAPPED": 1,
                "META_JUDGMENT": 2,
            },
        }
        for stage, expected in expected_counts.items():
            with self.subTest(stage=stage):
                store = new_dev_local_store(LOCAL_SETTINGS)
                database = DevMemoryDatabase(LOCAL_SETTINGS, store)
                dev_case = seed_dev_case(
                    LOCAL_SETTINGS,
                    database,
                    "weekend_plan",
                    "ARBITRATING",
                )
                reservation = database.claim_artifact(
                    dev_case.case_id,
                    "FINAL_JUDGMENT",
                )
                calls = {}
                mock = MockLLM(
                    LOCAL_SETTINGS,
                    get_fixture("weekend_plan"),
                    calls,
                    {"stage": stage, "triggered": False},
                )
                with self.assertRaises(LLMError):
                    run_final_arbitration(
                        database=database,
                        ask_llm=mock,
                        case_id=dev_case.case_id,
                        reservation_id=reservation,
                        sleep=lambda _seconds: None,
                    )
                run_final_arbitration(
                    database=database,
                    ask_llm=mock,
                    case_id=dev_case.case_id,
                    reservation_id=reservation,
                    sleep=lambda _seconds: None,
                )
                self.assertEqual(calls, expected)
                self.assertEqual(
                    database.get_case(dev_case.case_id)["status"],
                    "CLOSED",
                )

    def test_local_final_database_failure_resumes_without_llm_repeats(self):
        dev_case = seed_dev_case(
            LOCAL_SETTINGS,
            self.database,
            "weekend_plan",
            "ARBITRATING",
        )
        reservation = self.database.claim_artifact(
            dev_case.case_id,
            "FINAL_JUDGMENT",
        )
        calls = {}
        mock = MockLLM(
            LOCAL_SETTINGS,
            get_fixture("weekend_plan"),
            calls,
            {"stage": "NONE", "triggered": False},
        )
        failure = {
            "stage": "FINAL_DB_COMPLETE",
            "triggered": False,
            "attempts": 0,
        }
        wrapped = FinalCompleteFailureDatabase(
            LOCAL_SETTINGS,
            self.database,
            failure,
        )
        with self.assertRaises(DatabaseUnavailable):
            run_final_arbitration(
                database=wrapped,
                ask_llm=mock,
                case_id=dev_case.case_id,
                reservation_id=reservation,
                sleep=lambda _seconds: None,
            )
        before_resume = dict(calls)
        run_final_arbitration(
            database=wrapped,
            ask_llm=mock,
            case_id=dev_case.case_id,
            reservation_id=reservation,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(calls, before_resume)
        self.assertEqual(
            self.database.get_case(dev_case.case_id)["status"],
            "CLOSED",
        )

    def test_reset_clears_only_local_store(self):
        seed_dev_case(
            LOCAL_SETTINGS,
            self.database,
            "weekend_plan",
            "CLOSED",
        )
        self.assertTrue(self.store["cases"])
        self.database.reset()
        self.assertEqual(self.store["cases"], {})
        self.assertEqual(self.store["statements"], {})
        self.assertEqual(self.store["messages"], {})
        self.assertEqual(self.store["artifacts"], {})
        self.assertEqual(self.store["notifications"], {})
        self.assertEqual(self.store["message_sequence"], 0)

    def test_dispute_map_failure_requires_one_explicit_retry(self):
        dev_case = seed_dev_case(
            LOCAL_SETTINGS,
            self.database,
            "weekend_plan",
            "STATEMENTS_SUBMITTED",
        )
        case_id = dev_case.case_id
        reservation = self.database.claim_artifact(case_id, "DISPUTE_MAP")
        self.assertIsNotNone(reservation)
        self.assertTrue(
            self.database.fail_artifact(
                case_id,
                reservation,
                "DISPUTE_MAP",
            )
        )
        self.assertIsNone(self.database.claim_artifact(case_id, "DISPUTE_MAP"))

        retry = self.database.retry_failed_artifact(case_id, "DISPUTE_MAP")
        self.assertEqual(retry, reservation)
        self.assertIsNone(
            self.database.retry_failed_artifact(case_id, "DISPUTE_MAP")
        )
        self.database.complete_artifact(
            case_id,
            retry,
            "DISPUTE_MAP",
            "Recovered map",
        )
        self.assertEqual(self.database.get_case(case_id)["status"], "MAP_READY")
        self.assertEqual(
            self.database.get_artifact(case_id, "DISPUTE_MAP")["content"],
            "Recovered map",
        )

    def test_arbitration_notifications_are_recipient_scoped_and_acknowledged(self):
        dev_case = seed_dev_case(
            LOCAL_SETTINGS,
            self.database,
            "weekend_plan",
            "MEDIATING",
        )
        case_id = dev_case.case_id

        self.database.request_arbitration(case_id, "A")
        self.database.cancel_arbitration_request(case_id, "A")
        self.assertEqual(self.database.get_unread_notifications(case_id, "A"), [])

        self.database.request_arbitration(case_id, "A")
        self.database.cancel_arbitration_request(case_id, "B")
        declined = self.database.get_unread_notifications(case_id, "A")
        self.assertEqual(len(declined), 1)
        self.assertEqual(declined[0]["event_type"], "ARBITRATION_DECLINED")
        self.assertEqual(declined[0]["actor_role"], "B")
        self.assertEqual(self.database.get_unread_notifications(case_id, "B"), [])
        self.assertFalse(
            self.database.mark_notification_read(
                case_id,
                declined[0]["id"],
                "B",
            )
        )
        self.assertTrue(
            self.database.mark_notification_read(
                case_id,
                declined[0]["id"],
                "A",
            )
        )
        self.assertEqual(self.database.get_unread_notifications(case_id, "A"), [])

        self.database.request_arbitration(case_id, "A")
        self.database.confirm_arbitration(case_id, "B")
        self.database.confirm_arbitration(case_id, "B")
        accepted = self.database.get_unread_notifications(case_id, "A")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["event_type"], "ARBITRATION_ACCEPTED")
        self.assertEqual(accepted[0]["actor_role"], "B")


if __name__ == "__main__":
    unittest.main()
