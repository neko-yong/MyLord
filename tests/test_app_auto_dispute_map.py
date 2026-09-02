import os
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from llm import LLMError, LLMResult


class AutoMapDatabase:
    def __init__(self):
        self.status = "READY_FOR_MAP"
        self.artifact = None
        self.claim_calls = 0
        self.complete_calls = 0
        self.fail_calls = 0

    def init_db(self):
        return None

    def close(self):
        return None

    def get_case(self, _case_id):
        return {
            "case_id": "CASE-AUTO-MAP",
            "title": "Automatic dispute map test",
            "status": self.status,
            "paused_by": None,
            "arbitration_requested_by": None,
            "arbitration_requested_at": None,
            "arbitration_started_at": None,
        }

    def get_case_overview(self, case_id):
        return {
            "case": self.get_case(case_id),
            "submitted": {"A": True, "B": True},
        }

    def get_unread_notifications(self, _case_id, _role):
        return []

    def get_case_revision(self, case_id, _role):
        artifact_state = None
        if self.artifact:
            artifact_state = (
                self.artifact.get("id"),
                self.artifact.get("content"),
                self.artifact.get("generation_failed_at"),
            )
        return {
            "status": self.get_case(case_id)["status"],
            "artifact": artifact_state,
        }

    def get_case_view_snapshot(
        self,
        case_id,
        role,
        view,
        _last_message_id=0,
    ):
        artifacts = {}
        if self.artifact and (
            view in {"dispute", "mediation", "final"}
            or self.status == "READY_FOR_MAP"
        ):
            artifacts["DISPUTE_MAP"] = self.artifact
        return {
            "case": self.get_case(case_id),
            "submitted": {"A": True, "B": True},
            "statement": (
                self.get_statement(case_id, role)
                if view == "statement"
                else None
            ),
            "artifacts": artifacts,
            "evidence": None,
            "messages": [],
            "unread_notifications": [],
            "revision": self.get_case_revision(case_id, role),
        }

    def get_statement(self, _case_id, role):
        return {"content": f"{role} private statement"}

    def get_artifact(self, _case_id, kind):
        return self.artifact if kind == "DISPUTE_MAP" else None

    def get_statements_for_llm(self, _case_id):
        return {"A": "A private statement", "B": "B private statement"}

    def claim_artifact(self, _case_id, kind):
        self.claim_calls += 1
        if kind != "DISPUTE_MAP" or self.artifact is not None:
            return None
        self.artifact = {
            "id": 1,
            "kind": kind,
            "content": "",
            "generation_failed_at": None,
        }
        return 1

    def complete_artifact(self, _case_id, artifact_id, kind, content):
        self.complete_calls += 1
        if artifact_id != 1 or kind != "DISPUTE_MAP":
            raise AssertionError("unexpected artifact completion")
        self.artifact["content"] = content
        self.artifact["generation_failed_at"] = None
        self.status = "MAP_READY"

    def fail_artifact(self, _case_id, artifact_id, kind):
        self.fail_calls += 1
        if artifact_id != 1 or kind != "DISPUTE_MAP":
            return False
        self.artifact["generation_failed_at"] = "failed"
        return True

    def retry_failed_artifact(self, _case_id, kind):
        if (
            kind != "DISPUTE_MAP"
            or not self.artifact
            or not self.artifact["generation_failed_at"]
        ):
            return None
        self.artifact["generation_failed_at"] = None
        return self.artifact["id"]


def app_environment(database, llm_side_effect):
    environment = dict(os.environ)
    environment.update(
        {
            "DATABASE_URL": f"postgresql://auto-map-test/{id(database)}",
            "LLM_ENDPOINT": "https://provider.example/chat/completions",
            "LLM_MODEL": "test-model",
            "LLM_API_KEY": "test-key",
            "DEV_MODE": "false",
        }
    )
    return (
        patch.dict(os.environ, environment, clear=True),
        patch(
            "streamlit.runtime.secrets.Secrets.load_if_toml_exists",
            return_value=False,
        ),
        patch("db.Database", return_value=database),
        patch("llm.call_llm", side_effect=llm_side_effect),
    )


def result(content="Automatic map"):
    return LLMResult(content, "test-model", "stop", 0, 0, 0, 1.0)


class AutomaticDisputeMapAppTests(unittest.TestCase):
    def run_app(self, database, llm_side_effect):
        app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
        app = AppTest.from_file(app_path)
        app.session_state["auth"] = {
            "case_id": "CASE-AUTO-MAP",
            "role": "A",
        }
        patches = app_environment(database, llm_side_effect)
        return app, patches

    def test_ready_for_map_claims_and_generates_without_manual_button(self):
        database = AutoMapDatabase()
        app, patches = self.run_app(database, [result()])
        with patches[0], patches[1], patches[2], patches[3] as llm:
            app.run(timeout=15)

        self.assertEqual(database.status, "MAP_READY")
        self.assertEqual(database.claim_calls, 1)
        self.assertEqual(database.complete_calls, 1)
        self.assertEqual(llm.call_count, 1)
        self.assertFalse(
            any(button.label == "生成争议地图" for button in app.button)
        )

    def test_failed_generation_stays_frozen_until_explicit_retry(self):
        database = AutoMapDatabase()
        app, patches = self.run_app(
            database,
            [LLMError("test_failure"), result("Recovered map")],
        )
        with patches[0], patches[1], patches[2], patches[3] as llm:
            app.run(timeout=15)
            self.assertEqual(database.status, "READY_FOR_MAP")
            self.assertEqual(database.fail_calls, 1)
            self.assertEqual(llm.call_count, 1)
            app.run(timeout=15)
            next(
                button
                for button in app.button
                if button.label == "重新尝试整理争议地图"
            ).click()
            app.run(timeout=15)

        self.assertEqual(database.status, "MAP_READY")
        self.assertEqual(database.complete_calls, 1)
        self.assertEqual(llm.call_count, 2)


if __name__ == "__main__":
    unittest.main()
