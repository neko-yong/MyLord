import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import database_resources
from tests.test_app_chat_fragment import ChatDatabase


CASE_ID = "CASE-INTEGRATION"
ROUTE_KEY = "integration-route-key"
MAINTENANCE_SECRET = "integration-maintenance-secret"


def inline_dialog(*_args, **_kwargs):
    return lambda function: function


def find(items, label):
    return next(item for item in items if item.label == label)


class IntegratedDatabase:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.metadata = {
            "case_id": CASE_ID,
            "status": "COLLECTING",
            "created_at": now,
            "updated_at": now,
        }
        self.snapshot_calls = []
        self.revision_calls = []
        self.admin_calls = []
        self.delete_calls = []
        self.deleted = False

    def get_case_view_snapshot(
        self,
        case_id,
        role,
        view,
        last_message_id=0,
    ):
        self.snapshot_calls.append((case_id, role, view, last_message_id))
        if self.deleted:
            return None
        return {
            "case": {
                **self.metadata,
                "title": "Perf and admin integration",
                "paused_by": None,
                "arbitration_requested_by": None,
                "arbitration_requested_at": None,
                "arbitration_started_at": None,
            },
            "submitted": {"A": False, "B": False},
            "statement": None,
            "artifacts": {},
            "evidence": None,
            "messages": [],
            "unread_notifications": [],
            "revision": {
                "status": "COLLECTING",
                "updated_at": self.metadata["updated_at"],
                "latest_message_id": 0,
                "artifact_count": 0,
                "latest_artifact_at": None,
                "latest_artifact_failure_at": None,
                "unread_count": 0,
                "first_unread_id": 0,
            },
        }

    def get_case_revision(self, case_id, role):
        self.revision_calls.append((case_id, role))
        return None if self.deleted else {"status": "COLLECTING"}

    def list_case_metadata(self, limit, offset):
        self.admin_calls.append(("list", limit, offset))
        cases = [] if self.deleted else [dict(self.metadata)]
        return {"total": len(cases), "cases": cases}

    def get_case_admin_metadata(self, case_id):
        self.admin_calls.append(("get", case_id))
        if self.deleted or case_id != CASE_ID:
            return None
        return dict(self.metadata)

    def delete_case_exact(self, case_id):
        self.admin_calls.append(("delete", case_id))
        self.delete_calls.append(case_id)
        if self.deleted or case_id != CASE_ID:
            return None
        self.deleted = True
        counts = {
            "cases": 1,
            "statements": 2,
            "artifacts": 1,
            "messages": 1,
            "case_notifications": 1,
        }
        return {
            "case_id": case_id,
            "deleted_counts": counts,
            "residual_counts": {name: 0 for name in counts},
            "residual": 0,
        }


class ChatAdminDatabase(ChatDatabase):
    def __init__(self):
        super().__init__(
            status="MAP_READY",
            initial_message_ids=(),
            inserted_message_ids=(41, 57, 88),
        )
        self.admin_calls = []
        self.metadata_created_at = datetime.now(timezone.utc)

    def _metadata(self):
        return {
            "case_id": "CASE-CHAT",
            "status": self.status,
            "created_at": self.metadata_created_at,
            "updated_at": self.updated_at,
        }

    def list_case_metadata(self, limit, offset):
        self.admin_calls.append(("list", limit, offset))
        rows = [self._metadata()]
        return {
            "total": len(rows),
            "cases": rows[offset : offset + limit],
        }

    def get_case_admin_metadata(self, case_id):
        self.admin_calls.append(("get", case_id))
        return self._metadata() if case_id == "CASE-CHAT" else None

    def delete_case_exact(self, case_id):
        self.admin_calls.append(("delete", case_id))
        raise AssertionError("Cross-feature smoke must not delete a case")


class PerfAdminIntegrationTests(unittest.TestCase):
    def setUp(self):
        database_resources.get_postgres_pool.clear()
        self.app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")

    def tearDown(self):
        database_resources.get_postgres_pool.clear()

    def _run(self, app, database):
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "DATABASE_URL",
                "LLM_ENDPOINT",
                "LLM_MODEL",
                "LLM_API_KEY",
                "ADMIN_CREATE_SECRET",
                "ADMIN_CONSOLE_ROUTE_KEY",
                "ADMIN_MAINTENANCE_SECRET",
                "DEV_MODE",
                "DEV_DATABASE_MODE",
                "LLM_MODE",
            }
        }
        environment.update(
            {
                "DATABASE_URL": f"postgresql://integration-test/{id(database)}",
                "ADMIN_CONSOLE_ROUTE_KEY": ROUTE_KEY,
                "ADMIN_MAINTENANCE_SECRET": MAINTENANCE_SECRET,
                "DEV_MODE": "false",
            }
        )
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "streamlit.runtime.secrets.Secrets.load_if_toml_exists",
                return_value=False,
            ),
            patch("db.Database", return_value=database),
            patch("llm.call_llm", side_effect=AssertionError("Real LLM called")),
            patch("streamlit.dialog", new=inline_dialog),
        ):
            app.run(timeout=15)

    def test_route_lifecycles_are_isolated_and_delete_invalidates_client(self):
        database = IntegratedDatabase()

        case_app = AppTest.from_file(self.app_path)
        case_app.session_state["auth"] = {"case_id": CASE_ID, "role": "A"}
        self._run(case_app, database)

        self.assertEqual(
            database.snapshot_calls,
            [(CASE_ID, "A", "statement", 0)],
        )
        self.assertEqual(database.admin_calls, [])

        admin_app = AppTest.from_file(self.app_path)
        admin_app.query_params["console"] = ROUTE_KEY
        self._run(admin_app, database)
        self.assertEqual(database.admin_calls, [])
        self.assertEqual(len(database.snapshot_calls), 1)

        find(admin_app.text_input, "Maintenance secret").set_value(
            MAINTENANCE_SECRET
        )
        find(admin_app.button, "Sign in").click()
        self._run(admin_app, database)
        self.assertEqual(database.admin_calls, [("list", 25, 0)])
        self.assertEqual(len(database.snapshot_calls), 1)

        self._run(admin_app, database)
        self.assertEqual(database.admin_calls[-1], ("list", 25, 0))
        self.assertFalse(database.deleted)
        self.assertEqual(len(database.snapshot_calls), 1)

        find(admin_app.text_input, "Full Case ID").set_value(CASE_ID)
        find(admin_app.button, "Find case").click()
        self._run(admin_app, database)
        find(admin_app.button, "Permanently delete").click()
        self._run(admin_app, database)
        confirmation = find(
            admin_app.checkbox,
            "我确认删除这个案件，且无法恢复",
        )
        confirmation.check()
        self._run(admin_app, database)
        find(admin_app.button, "Delete permanently").click()
        self._run(admin_app, database)

        self.assertTrue(database.deleted)
        self.assertEqual(database.delete_calls, [CASE_ID])
        self.assertEqual(len(database.snapshot_calls), 1)

        self._run(case_app, database)
        errors = "\n".join(str(element.value) for element in case_app.error)
        self.assertIn("案件不存在或已不可用", errors)
        self.assertEqual(len(database.snapshot_calls), 2)
        self.assertEqual(database.delete_calls, [CASE_ID])

    def test_a_b_chat_and_admin_refresh_search_are_isolated(self):
        database = ChatAdminDatabase()

        app_a = AppTest.from_file(self.app_path)
        app_a.session_state["auth"] = {"case_id": "CASE-CHAT", "role": "A"}
        app_a.session_state["case_tab"] = "③ 调解室"
        self._run(app_a, database)

        app_b = AppTest.from_file(self.app_path)
        app_b.session_state["auth"] = {"case_id": "CASE-CHAT", "role": "B"}
        app_b.session_state["case_tab"] = "③ 调解室"
        self._run(app_b, database)

        admin_app = AppTest.from_file(self.app_path)
        admin_app.query_params["console"] = ROUTE_KEY
        normal_snapshot_count = len(database.snapshot_views)
        self._run(admin_app, database)
        self.assertEqual(len(database.snapshot_views), normal_snapshot_count)

        find(admin_app.text_input, "Maintenance secret").set_value(
            MAINTENANCE_SECRET
        )
        find(admin_app.button, "Sign in").click()
        self._run(admin_app, database)
        self.assertEqual(database.admin_calls, [("list", 25, 0)])
        self.assertEqual(len(database.snapshot_views), normal_snapshot_count)

        admin_call_count = len(database.admin_calls)
        app_a.chat_input[0].set_value("message 1 from A")
        self._run(app_a, database)
        self.assertEqual(len(database.admin_calls), admin_call_count)
        self._run(app_b, database)
        visible_b = "\n".join(str(item.value) for item in app_b.markdown)
        self.assertIn("message 1 from A", visible_b)

        self._run(admin_app, database)
        find(admin_app.text_input, "Full Case ID").set_value("CASE-CHAT")
        find(admin_app.button, "Find case").click()
        self._run(admin_app, database)
        self.assertEqual(database.admin_calls[-1], ("get", "CASE-CHAT"))
        self.assertEqual(len(database.messages), 1)

        admin_call_count = len(database.admin_calls)
        app_b.chat_input[0].set_value("message 2 from B")
        self._run(app_b, database)
        self._run(app_a, database)
        visible_a = "\n".join(str(item.value) for item in app_a.markdown)
        self.assertIn("message 2 from B", visible_a)
        self.assertEqual(len(database.admin_calls), admin_call_count)

        app_a.chat_input[0].set_value("message 3 from A")
        self._run(app_a, database)
        self._run(app_b, database)
        visible_b = "\n".join(str(item.value) for item in app_b.markdown)
        self.assertIn("message 3 from A", visible_b)
        self.assertEqual(
            [message["id"] for message in database.messages],
            [41, 57, 88],
        )
        self.assertFalse(app_a.chat_input[0].disabled)
        self.assertFalse(app_b.chat_input[0].disabled)


if __name__ == "__main__":
    unittest.main()
