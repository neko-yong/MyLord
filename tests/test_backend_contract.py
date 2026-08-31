import unittest
from types import SimpleNamespace

from arbitration import run_final_arbitration
from db import CaseStateError, Database
from dev_fixtures import get_fixture
from dev_memory_db import DevMemoryDatabase, new_dev_local_store
from dev_tools import delete_dev_case, seed_dev_case
from integration_config import load_test_database_url
from mock_llm import MockLLM


LOCAL_SETTINGS = SimpleNamespace(dev_mode=True, dev_database_mode="local")
POSTGRES_SETTINGS = SimpleNamespace(dev_mode=True, dev_database_mode="postgres")
TEST_DATABASE_URL, _TEST_DATABASE_SOURCE = load_test_database_url()


def observable_workflow(database, settings):
    dev_case = seed_dev_case(
        settings,
        database,
        "weekend_plan",
        "MEDIATING",
    )
    case_id = dev_case.case_id
    try:
        initial = database.get_case(case_id)
        initial_messages = database.get_messages(case_id)

        paused = database.pause_case(case_id, "B")
        wrong_resume = database.resume_case(case_id, "A")
        resumed = database.resume_case(case_id, "B")

        database.request_arbitration(case_id, "A")
        pending = database.get_case(case_id)
        pending_message = database.add_message(
            case_id,
            "B",
            "Contract pending message",
        )
        pending_count = len(database.get_messages(case_id))
        database.cancel_arbitration_request(case_id, "B")
        declined = database.get_unread_notifications(case_id, "A")
        declined_ack = database.mark_notification_read(
            case_id,
            declined[0]["id"],
            "A",
        )
        after_cancel = database.get_case(case_id)

        database.request_arbitration(case_id, "A")
        evidence = database.confirm_arbitration(case_id, "B")
        accepted = database.get_unread_notifications(case_id, "A")
        arbitrating = database.get_case(case_id)
        frozen_messages = evidence["snapshot"]["messages"]
        try:
            database.add_message(case_id, "A", "must be rejected")
        except CaseStateError:
            frozen_write_rejected = True
        else:
            frozen_write_rejected = False

        reservation = database.claim_artifact(case_id, "FINAL_JUDGMENT")
        calls = {}
        run_final_arbitration(
            database=database,
            ask_llm=MockLLM(
                settings,
                get_fixture("weekend_plan"),
                calls,
                {"stage": "NONE", "triggered": False},
            ),
            case_id=case_id,
            reservation_id=reservation,
            sleep=lambda _seconds: None,
        )
        kinds = (
            "DISPUTE_MAP",
            "ARBITRATION_EVIDENCE",
            "JUDGMENT_NORMAL",
            "JUDGMENT_SWAPPED",
            "META_JUDGMENT",
            "FINAL_JUDGMENT",
        )
        return {
            "initial_status": initial["status"],
            "initial_requester": initial["arbitration_requested_by"],
            "initial_paused_by": initial["paused_by"],
            "initial_message_count": len(initial_messages),
            "paused": paused,
            "wrong_resume": wrong_resume,
            "resumed": resumed,
            "pending_status": pending["status"],
            "pending_requester": pending["arbitration_requested_by"],
            "pending_message_delta": pending_count - len(initial_messages),
            "pending_message_keys": tuple(sorted(pending_message)),
            "pending_message_previous_matches": (
                pending_message["previous_message_id"]
                == initial_messages[-1]["id"]
            ),
            "pending_message_case_status": pending_message["case_status"],
            "cancel_status": after_cancel["status"],
            "declined_notification": declined[0]["event_type"],
            "declined_ack": declined_ack,
            "accepted_notification": accepted[0]["event_type"],
            "arbitrating_status": arbitrating["status"],
            "arbitrating_requester": arbitrating["arbitration_requested_by"],
            "evidence_cutoff_is_last": (
                evidence["snapshot"]["message_cutoff_id"]
                == frozen_messages[-1]["id"]
            ),
            "evidence_message_count": len(frozen_messages),
            "evidence_hash_present": bool(evidence["evidence_hash"]),
            "frozen_write_rejected": frozen_write_rejected,
            "artifact_kinds": tuple(
                kind for kind in kinds if database.get_artifact(case_id, kind)
            ),
            "mock_calls": calls,
            "final_status": database.get_case(case_id)["status"],
        }
    finally:
        delete_dev_case(settings, database, case_id)


class BackendContractTests(unittest.TestCase):
    def test_fast_local_observable_contract(self):
        database = DevMemoryDatabase(
            LOCAL_SETTINGS,
            new_dev_local_store(LOCAL_SETTINGS),
        )
        result = observable_workflow(database, LOCAL_SETTINGS)
        self.assertEqual(result["initial_status"], "MEDIATING")
        self.assertEqual(result["pending_status"], "ARBITRATION_PENDING")
        self.assertEqual(result["pending_requester"], "A")
        self.assertEqual(result["pending_message_delta"], 1)
        self.assertTrue(result["pending_message_previous_matches"])
        self.assertEqual(
            result["pending_message_case_status"],
            "ARBITRATION_PENDING",
        )
        self.assertIn("previous_message_id", result["pending_message_keys"])
        self.assertEqual(result["cancel_status"], "MEDIATING")
        self.assertEqual(result["declined_notification"], "ARBITRATION_DECLINED")
        self.assertTrue(result["declined_ack"])
        self.assertEqual(result["accepted_notification"], "ARBITRATION_ACCEPTED")
        self.assertEqual(result["arbitrating_status"], "ARBITRATING")
        self.assertTrue(result["evidence_cutoff_is_last"])
        self.assertTrue(result["frozen_write_rejected"])
        self.assertEqual(result["final_status"], "CLOSED")

    @unittest.skipUnless(
        TEST_DATABASE_URL,
        "POSTGRES_CONTRACT = NOT RUN (TEST_DATABASE_URL is not set)",
    )
    def test_memory_matches_postgres_observable_contract(self):
        memory = DevMemoryDatabase(
            LOCAL_SETTINGS,
            new_dev_local_store(LOCAL_SETTINGS),
        )
        expected = observable_workflow(memory, LOCAL_SETTINGS)
        postgres = Database(
            TEST_DATABASE_URL,
            min_size=1,
            max_size=2,
        )
        try:
            postgres.init_db()
            actual = observable_workflow(postgres, POSTGRES_SETTINGS)
        finally:
            postgres.close()
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
