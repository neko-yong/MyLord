import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace

from arbitration import run_final_arbitration
from db import CaseStateError, DatabaseUnavailable
from dev_tools import (
    DEV_TITLE_PREFIX,
    FinalCompleteFailureDatabase,
    SCENARIOS,
    delete_dev_case,
    get_dev_state,
    recreate_dev_case,
    seed_dev_case,
    switch_dev_role,
)
from mock_llm import MockLLM
from dev_fixtures import get_fixture


DEV_SETTINGS = SimpleNamespace(dev_mode=True)
PROD_SETTINGS = SimpleNamespace(dev_mode=False)


class MemoryDatabase:
    def __init__(self):
        self.cases = {}
        self.statements = {}
        self.artifacts = {}
        self.messages = {}
        self.next_case = 1
        self.next_artifact = 1
        self.next_message = 1
        self.calls = []

    def create_case(self, title):
        case_id = f"CASE-DEV{self.next_case:02d}"
        self.next_case += 1
        self.cases[case_id] = {
            "case_id": case_id,
            "title": title,
            "status": "COLLECTING",
            "paused_by": None,
            "arbitration_requested_by": None,
            "arbitration_requested_at": None,
            "arbitration_started_at": None,
        }
        self.messages[case_id] = []
        self.calls.append("create_case")
        return case_id, "A-dev-raw-token", "B-dev-raw-token"

    def get_case(self, case_id):
        return self.cases.get(case_id)

    def get_submission_status(self, case_id):
        return {
            role: (case_id, role) in self.statements for role in ("A", "B")
        }

    def save_statement(self, case_id, role, content):
        self.calls.append(f"save_statement_{role}")
        self.statements[(case_id, role)] = content
        if all((case_id, role) in self.statements for role in ("A", "B")):
            self.cases[case_id]["status"] = "READY_FOR_MAP"

    def claim_artifact(self, case_id, kind):
        self.calls.append(f"claim_{kind}")
        if (case_id, kind) in self.artifacts:
            return None
        expected = "READY_FOR_MAP" if kind == "DISPUTE_MAP" else "ARBITRATING"
        if self.cases[case_id]["status"] != expected:
            return None
        artifact_id = self.next_artifact
        self.next_artifact += 1
        self.artifacts[(case_id, kind)] = {
            "id": artifact_id,
            "content": "",
            "evidence_hash": (
                self.artifacts[(case_id, "ARBITRATION_EVIDENCE")][
                    "evidence_hash"
                ]
                if kind == "FINAL_JUDGMENT"
                else None
            ),
        }
        return artifact_id

    def complete_artifact(self, case_id, artifact_id, kind, content):
        self.calls.append(f"complete_{kind}")
        artifact = self.artifacts[(case_id, kind)]
        if artifact["id"] != artifact_id:
            raise AssertionError("wrong artifact")
        artifact["content"] = content
        self.cases[case_id]["status"] = (
            "MAP_READY" if kind == "DISPUTE_MAP" else "CLOSED"
        )

    def release_artifact(self, case_id, artifact_id, kind):
        artifact = self.artifacts.get((case_id, kind))
        if artifact and artifact["id"] == artifact_id and not artifact["content"]:
            del self.artifacts[(case_id, kind)]

    def get_artifact(self, case_id, kind):
        return self.artifacts.get((case_id, kind))

    def add_message(self, case_id, sender, content):
        if self.cases[case_id]["status"] not in {
            "MAP_READY",
            "MEDIATING",
            "ARBITRATION_PENDING",
        }:
            raise CaseStateError("messages locked")
        self.calls.append(f"add_message_{sender}")
        message = {
            "id": self.next_message,
            "sender": sender,
            "content": content,
            "created_at": "2026-08-25T12:00:00+00:00",
        }
        self.next_message += 1
        self.messages[case_id].append(message)
        if self.cases[case_id]["status"] == "MAP_READY":
            self.cases[case_id]["status"] = "MEDIATING"

    def get_messages(self, case_id):
        return list(self.messages.get(case_id, []))

    def pause_case(self, case_id, role):
        self.calls.append("pause_case")
        if self.cases[case_id]["status"] not in {"MAP_READY", "MEDIATING"}:
            return False
        self.cases[case_id]["status"] = "PAUSED"
        self.cases[case_id]["paused_by"] = role
        return True

    def request_arbitration(self, case_id, role):
        self.calls.append(f"request_arbitration_{role}")
        case = self.cases[case_id]
        if case["status"] not in {"MAP_READY", "MEDIATING"}:
            raise CaseStateError("request blocked")
        case["status"] = "ARBITRATION_PENDING"
        case["arbitration_requested_by"] = role
        return case

    def confirm_arbitration(self, case_id, role):
        self.calls.append(f"confirm_arbitration_{role}")
        case = self.cases[case_id]
        requester = case["arbitration_requested_by"]
        if case["status"] != "ARBITRATION_PENDING" or requester == role:
            raise CaseStateError("confirmation blocked")
        snapshot = {
            "version": 1,
            "created_at": "2026-08-25T12:01:00+00:00",
            "case_id": case_id,
            "a_statement": self.statements[(case_id, "A")],
            "b_statement": self.statements[(case_id, "B")],
            "dispute_map": self.artifacts[(case_id, "DISPUTE_MAP")]["content"],
            "messages": self.get_messages(case_id),
            "message_cutoff_id": (
                self.messages[case_id][-1]["id"] if self.messages[case_id] else 0
            ),
            "requester": requester,
            "confirmer": role,
        }
        evidence = {
            "id": self.next_artifact,
            "content": "canonical evidence",
            "evidence_hash": "a" * 64,
            "snapshot": snapshot,
        }
        self.next_artifact += 1
        self.artifacts[(case_id, "ARBITRATION_EVIDENCE")] = evidence
        case["status"] = "ARBITRATING"
        return evidence

    def get_arbitration_evidence(self, case_id):
        return self.artifacts.get((case_id, "ARBITRATION_EVIDENCE"))

    def save_checkpoint(self, case_id, kind, content, evidence_hash):
        self.calls.append(f"save_{kind}")
        existing = self.artifacts.get((case_id, kind))
        if existing:
            return existing["id"]
        artifact = {
            "id": self.next_artifact,
            "content": content,
            "evidence_hash": evidence_hash,
        }
        self.next_artifact += 1
        self.artifacts[(case_id, kind)] = artifact
        return artifact["id"]

    def delete_case_if_title_prefix(self, case_id, prefix):
        self.calls.append("delete_case_if_title_prefix")
        case = self.cases.get(case_id)
        if not case or not case["title"].startswith(prefix):
            return False
        del self.cases[case_id]
        self.messages.pop(case_id, None)
        self.statements = {
            key: value for key, value in self.statements.items() if key[0] != case_id
        }
        self.artifacts = {
            key: value for key, value in self.artifacts.items() if key[0] != case_id
        }
        return True


class DevScenarioTests(unittest.TestCase):
    def setUp(self):
        self.database = MemoryDatabase()

    def seed(self, scenario):
        return seed_dev_case(
            DEV_SETTINGS,
            self.database,
            "weekend_plan",
            scenario,
        )

    def test_all_required_scenarios_are_supported(self):
        self.assertEqual(
            set(SCENARIOS),
            {
                "EMPTY",
                "A_SUBMITTED",
                "STATEMENTS_SUBMITTED",
                "MAP_READY",
                "MEDIATING",
                "PAUSED",
                "ARBITRATION_PENDING_A",
                "ARBITRATION_PENDING_B",
                "ARBITRATING",
                "CLOSED",
            },
        )

    def test_each_scenario_has_consistent_state_and_artifacts(self):
        expectations = {
            "EMPTY": ("COLLECTING", 0, False, 0, False, False),
            "A_SUBMITTED": ("COLLECTING", 1, False, 0, False, False),
            "STATEMENTS_SUBMITTED": (
                "READY_FOR_MAP",
                2,
                False,
                0,
                False,
                False,
            ),
            "MAP_READY": ("MAP_READY", 2, True, 0, False, False),
            "MEDIATING": ("MEDIATING", 2, True, 2, False, False),
            "PAUSED": ("PAUSED", 2, True, 2, False, False),
            "ARBITRATION_PENDING_A": (
                "ARBITRATION_PENDING",
                2,
                True,
                2,
                False,
                False,
            ),
            "ARBITRATION_PENDING_B": (
                "ARBITRATION_PENDING",
                2,
                True,
                2,
                False,
                False,
            ),
            "ARBITRATING": ("ARBITRATING", 2, True, 2, True, False),
            "CLOSED": ("CLOSED", 2, True, 2, True, True),
        }

        for scenario, expected in expectations.items():
            with self.subTest(scenario=scenario):
                database = MemoryDatabase()
                dev_case = seed_dev_case(
                    DEV_SETTINGS,
                    database,
                    "weekend_plan",
                    scenario,
                )
                state = get_dev_state(
                    DEV_SETTINGS,
                    database,
                    dev_case.case_id,
                )
                actual = (
                    state["status"],
                    sum((state["a_submitted"], state["b_submitted"])),
                    state["dispute_map"],
                    state["message_count"],
                    state["evidence"],
                    state["final"],
                )
                self.assertEqual(actual, expected)

                if scenario == "PAUSED":
                    self.assertEqual(state["paused_by"], "B")
                if scenario.endswith("_A"):
                    self.assertEqual(state["arbitration_request"], "A")
                if scenario.endswith("_B"):
                    self.assertEqual(state["arbitration_request"], "B")
                if scenario == "ARBITRATING":
                    with self.assertRaises(CaseStateError):
                        database.add_message(dev_case.case_id, "A", "blocked")
                if scenario == "CLOSED":
                    self.assertTrue(state["judgment_normal"])
                    self.assertTrue(state["judgment_swapped"])
                    self.assertTrue(state["meta"])
                    self.assertEqual(
                        dev_case.seed_mock_calls,
                        {
                            "JUDGMENT_NORMAL": 1,
                            "JUDGMENT_SWAPPED": 1,
                            "META_JUDGMENT": 1,
                        },
                    )

    def test_scenario_builder_does_not_contain_direct_status_sql(self):
        source = inspect.getsource(seed_dev_case).upper()

        self.assertNotIn("UPDATE CASES", source)
        self.assertNotIn("SET STATUS", source)

    def test_developer_enablement_has_no_frontend_bypass(self):
        project = Path(__file__).resolve().parents[1]
        source = "\n".join(
            (project / name).read_text(encoding="utf-8")
            for name in ("app.py", "dev_tools.py", "mock_llm.py")
        )

        self.assertNotIn("query_params", source)
        self.assertNotIn("?dev=true", source.lower())
        self.assertNotIn("?debug=true", source.lower())
        self.assertNotIn("ADMIN_CREATE_SECRET", source)

    def test_production_blocks_seed_role_switch_state_and_delete(self):
        for operation in (
            lambda: seed_dev_case(
                PROD_SETTINGS,
                self.database,
                "weekend_plan",
                "EMPTY",
            ),
            lambda: switch_dev_role(
                PROD_SETTINGS,
                self.database,
                "CASE-ANY",
                "A",
            ),
            lambda: get_dev_state(
                PROD_SETTINGS,
                self.database,
                "CASE-ANY",
            ),
            lambda: delete_dev_case(
                PROD_SETTINGS,
                self.database,
                "CASE-ANY",
            ),
        ):
            with self.subTest(operation=operation):
                before = list(self.database.calls)
                with self.assertRaises(PermissionError):
                    operation()
                self.assertEqual(self.database.calls, before)

    def test_role_switch_and_delete_require_dev_case(self):
        dev_case = self.seed("MEDIATING")
        self.database.cases["CASE-REAL"] = {
            "case_id": "CASE-REAL",
            "title": "ordinary case",
            "status": "MEDIATING",
            "paused_by": None,
            "arbitration_requested_by": None,
        }

        self.assertEqual(
            switch_dev_role(
                DEV_SETTINGS,
                self.database,
                dev_case.case_id,
                "B",
            ),
            "B",
        )
        for operation in (
            lambda: switch_dev_role(
                DEV_SETTINGS,
                self.database,
                "CASE-REAL",
                "A",
            ),
            lambda: delete_dev_case(
                DEV_SETTINGS,
                self.database,
                "CASE-REAL",
            ),
        ):
            with self.assertRaises(PermissionError):
                operation()
        self.assertIn("CASE-REAL", self.database.cases)

        self.assertTrue(
            delete_dev_case(
                DEV_SETTINGS,
                self.database,
                dev_case.case_id,
            )
        )
        self.assertNotIn(dev_case.case_id, self.database.cases)

    def test_recreate_deletes_old_case_and_seeds_new_case(self):
        first = self.seed("PAUSED")

        second = recreate_dev_case(
            DEV_SETTINGS,
            self.database,
            first.case_id,
            first.fixture_key,
            first.scenario,
        )

        self.assertNotEqual(first.case_id, second.case_id)
        self.assertNotIn(first.case_id, self.database.cases)
        self.assertEqual(
            self.database.cases[second.case_id]["status"],
            "PAUSED",
        )


class DevCheckpointTests(unittest.TestCase):
    def seed_arbitrating(self):
        database = MemoryDatabase()
        dev_case = seed_dev_case(
            DEV_SETTINGS,
            database,
            "weekend_plan",
            "ARBITRATING",
        )
        return database, dev_case

    def test_j2_failure_resume_does_not_repeat_j1(self):
        database, dev_case = self.seed_arbitrating()
        reservation = database.claim_artifact(
            dev_case.case_id,
            "FINAL_JUDGMENT",
        )
        calls = {}
        failure = {"stage": "JUDGMENT_SWAPPED", "triggered": False}
        mock = MockLLM(
            DEV_SETTINGS,
            get_fixture("weekend_plan"),
            calls,
            failure,
        )

        with self.assertRaises(Exception):
            run_final_arbitration(
                database,
                mock,
                dev_case.case_id,
                reservation,
            )
        database.release_artifact(
            dev_case.case_id,
            reservation,
            "FINAL_JUDGMENT",
        )
        resumed_reservation = database.claim_artifact(
            dev_case.case_id,
            "FINAL_JUDGMENT",
        )
        run_final_arbitration(
            database,
            mock,
            dev_case.case_id,
            resumed_reservation,
        )

        self.assertEqual(calls["JUDGMENT_NORMAL"], 1)
        self.assertEqual(calls["JUDGMENT_SWAPPED"], 2)
        self.assertEqual(calls["META_JUDGMENT"], 1)

    def test_j1_failure_resume_retries_only_missing_chain(self):
        database, dev_case = self.seed_arbitrating()
        reservation = database.claim_artifact(
            dev_case.case_id,
            "FINAL_JUDGMENT",
        )
        calls = {}
        failure = {"stage": "JUDGMENT_NORMAL", "triggered": False}
        mock = MockLLM(
            DEV_SETTINGS,
            get_fixture("weekend_plan"),
            calls,
            failure,
        )

        with self.assertRaises(Exception):
            run_final_arbitration(
                database,
                mock,
                dev_case.case_id,
                reservation,
            )
        database.release_artifact(
            dev_case.case_id,
            reservation,
            "FINAL_JUDGMENT",
        )
        resumed = database.claim_artifact(
            dev_case.case_id,
            "FINAL_JUDGMENT",
        )
        run_final_arbitration(database, mock, dev_case.case_id, resumed)

        self.assertEqual(calls["JUDGMENT_NORMAL"], 2)
        self.assertEqual(calls["JUDGMENT_SWAPPED"], 1)
        self.assertEqual(calls["META_JUDGMENT"], 1)

    def test_meta_failure_resume_does_not_repeat_j1_or_j2(self):
        database, dev_case = self.seed_arbitrating()
        reservation = database.claim_artifact(
            dev_case.case_id,
            "FINAL_JUDGMENT",
        )
        calls = {}
        failure = {"stage": "META_JUDGMENT", "triggered": False}
        mock = MockLLM(
            DEV_SETTINGS,
            get_fixture("weekend_plan"),
            calls,
            failure,
        )

        with self.assertRaises(Exception):
            run_final_arbitration(
                database,
                mock,
                dev_case.case_id,
                reservation,
            )
        database.release_artifact(
            dev_case.case_id,
            reservation,
            "FINAL_JUDGMENT",
        )
        resumed = database.claim_artifact(
            dev_case.case_id,
            "FINAL_JUDGMENT",
        )
        run_final_arbitration(database, mock, dev_case.case_id, resumed)

        self.assertEqual(calls["JUDGMENT_NORMAL"], 1)
        self.assertEqual(calls["JUDGMENT_SWAPPED"], 1)
        self.assertEqual(calls["META_JUDGMENT"], 2)

    def test_final_database_failure_resumes_without_repeating_llm(self):
        database, dev_case = self.seed_arbitrating()
        reservation = database.claim_artifact(
            dev_case.case_id,
            "FINAL_JUDGMENT",
        )
        calls = {}
        failure = {
            "stage": "FINAL_DB_COMPLETE",
            "triggered": False,
            "attempts": 0,
        }
        proxy = FinalCompleteFailureDatabase(
            DEV_SETTINGS,
            database,
            failure,
        )
        mock = MockLLM(
            DEV_SETTINGS,
            get_fixture("weekend_plan"),
            calls,
            failure,
        )

        with self.assertRaises(DatabaseUnavailable):
            run_final_arbitration(
                proxy,
                mock,
                dev_case.case_id,
                reservation,
                sleep=lambda _seconds: None,
            )
        before_resume = dict(calls)
        run_final_arbitration(
            proxy,
            mock,
            dev_case.case_id,
            reservation,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(calls, before_resume)
        self.assertEqual(database.get_case(dev_case.case_id)["status"], "CLOSED")

    def test_production_blocks_final_database_injection(self):
        with self.assertRaises(PermissionError):
            FinalCompleteFailureDatabase(
                PROD_SETTINGS,
                MemoryDatabase(),
                {"stage": "FINAL_DB_COMPLETE"},
            )


if __name__ == "__main__":
    unittest.main()
