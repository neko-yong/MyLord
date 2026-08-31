import os
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


def inline_dialog(*_args, **_kwargs):
    return lambda function: function


def find(items, label):
    return next(item for item in items if item.label == label)


class ChatDatabase:
    def __init__(
        self,
        *,
        status="MEDIATING",
        initial_message_ids=(1,),
        inserted_message_ids=(),
    ):
        self.snapshot_views = []
        self.revision_calls = 0
        self.write_calls = 0
        self.pause_calls = 0
        self.resume_calls = 0
        self.status = status
        self.paused_by = None
        self.messages = [
            {
                "id": message_id,
                "case_id": "CASE-CHAT",
                "sender": "B",
                "content": "existing message",
                "created_at": "before",
            }
            for message_id in initial_message_ids
        ]
        self.inserted_message_ids = list(inserted_message_ids)
        self.updated_at = "before"

    def init_db(self):
        return None

    def close(self):
        return None

    def get_case_revision(self, _case_id, _role):
        self.revision_calls += 1
        return {
            "status": self.status,
            "updated_at": self.updated_at,
            "latest_message_id": (
                self.messages[-1]["id"] if self.messages else 0
            ),
            "artifact_count": 1,
            "latest_artifact_at": "before",
            "latest_artifact_failure_at": None,
            "unread_count": 0,
            "first_unread_id": 0,
        }

    def get_case_view_snapshot(
        self,
        case_id,
        role,
        view,
        last_message_id=0,
    ):
        self.snapshot_views.append(view)
        return {
            "case": {
                "case_id": case_id,
                "title": "Chat fragment test",
                "status": self.status,
                "paused_by": self.paused_by,
                "arbitration_requested_by": None,
                "arbitration_requested_at": None,
                "arbitration_started_at": None,
                "updated_at": self.updated_at,
            },
            "submitted": {"A": True, "B": True},
            "statement": (
                {"content": "A statement"}
                if view == "statement"
                else None
            ),
            "artifacts": {
                "DISPUTE_MAP": {
                    "id": 1,
                    "kind": "DISPUTE_MAP",
                    "content": "dispute map",
                    "generation_failed_at": None,
                }
            },
            "evidence": None,
            "messages": [
                message
                for message in self.messages
                if view == "mediation" and message["id"] > last_message_id
            ],
            "unread_notifications": [],
            "revision": self.get_case_revision(case_id, role),
        }

    def add_message(self, case_id, sender, content):
        self.write_calls += 1
        self.updated_at = f"after-{self.write_calls}"
        previous_message_id = (
            self.messages[-1]["id"] if self.messages else 0
        )
        if self.inserted_message_ids:
            message_id = self.inserted_message_ids.pop(0)
        else:
            message_id = self.messages[-1]["id"] + 1 if self.messages else 1
        if self.status == "MAP_READY":
            self.status = "MEDIATING"
        message = {
            "id": message_id,
            "case_id": case_id,
            "sender": sender,
            "content": content,
            "created_at": self.updated_at,
            "case_status": self.status,
            "case_updated_at": self.updated_at,
            "previous_message_id": previous_message_id,
        }
        self.messages.append(message)
        return message

    def pause_case(self, _case_id, role):
        self.pause_calls += 1
        if self.status not in {"MAP_READY", "MEDIATING"}:
            return False
        self.status = "PAUSED"
        self.paused_by = role
        self.updated_at = f"paused-{self.pause_calls}"
        return True

    def resume_case(self, _case_id, role):
        self.resume_calls += 1
        if self.status != "PAUSED" or self.paused_by != role:
            return False
        self.status = "MEDIATING"
        self.paused_by = None
        self.updated_at = f"resumed-{self.resume_calls}"
        return True


class StaleCacheDatabase(ChatDatabase):
    def add_message(self, case_id, sender, content):
        message = super().add_message(case_id, sender, content)
        message["previous_message_id"] = -1
        return message


class ChatFragmentTests(unittest.TestCase):
    def test_normal_chat_is_written_once_and_uses_database_result(self):
        database = ChatDatabase()
        app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
        environment = dict(os.environ)
        environment.update(
            {
                "DATABASE_URL": f"postgresql://chat-test/{id(database)}",
                "LLM_ENDPOINT": "",
                "LLM_MODEL": "",
                "LLM_API_KEY": "",
            }
        )
        patches = (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "streamlit.runtime.secrets.Secrets.load_if_toml_exists",
                return_value=False,
            ),
            patch("db.Database", return_value=database),
            patch("streamlit.dialog", new=inline_dialog),
        )

        with patches[0], patches[1], patches[2], patches[3]:
            app = AppTest.from_file(app_path)
            app.session_state["auth"] = {
                "case_id": "CASE-CHAT",
                "role": "A",
            }
            app.session_state["case_tab"] = "③ 调解室"
            app.run(timeout=15)
            self.assertEqual(database.snapshot_views, ["mediation"])

            app.chat_input[0].set_value("new message").run(timeout=15)

        self.assertEqual(database.write_calls, 1)
        self.assertEqual(database.snapshot_views, ["mediation", "mediation"])
        visible = "\n".join(str(item.value) for item in app.markdown)
        self.assertIn("new message", visible)

    def test_first_and_non_contiguous_messages_do_not_force_app_rerun(self):
        database = ChatDatabase(
            status="MAP_READY",
            initial_message_ids=(),
            inserted_message_ids=(41, 57),
        )
        rerun_scopes = []

        def record_rerun(*, scope="app"):
            rerun_scopes.append(scope)

        app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
        environment = dict(os.environ)
        environment.update(
            {
                "DATABASE_URL": f"postgresql://chat-test/{id(database)}",
                "LLM_ENDPOINT": "",
                "LLM_MODEL": "",
                "LLM_API_KEY": "",
            }
        )

        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "streamlit.runtime.secrets.Secrets.load_if_toml_exists",
                return_value=False,
            ),
            patch("db.Database", return_value=database),
            patch("streamlit.dialog", new=inline_dialog),
            patch("streamlit.rerun", side_effect=record_rerun),
        ):
            app = AppTest.from_file(app_path)
            app.session_state["auth"] = {
                "case_id": "CASE-CHAT",
                "role": "A",
            }
            app.session_state["case_tab"] = "③ 调解室"
            app.run(timeout=15)

            app.chat_input[0].set_value("message 1").run(timeout=15)
            visible = "\n".join(str(item.value) for item in app.markdown)
            self.assertIn("message 1", visible)
            self.assertFalse(app.chat_input[0].disabled)
            self.assertEqual(rerun_scopes, [])

            app.chat_input[0].set_value("message 2").run(timeout=15)
            visible = "\n".join(str(item.value) for item in app.markdown)
            self.assertIn("message 2", visible)

        self.assertEqual(database.write_calls, 2)
        self.assertEqual(rerun_scopes, [])

    def test_stale_cache_uses_incremental_snapshot_without_app_rerun(self):
        database = StaleCacheDatabase(
            initial_message_ids=(41,),
            inserted_message_ids=(88,),
        )
        rerun_scopes = []

        def record_rerun(*, scope="app"):
            rerun_scopes.append(scope)

        app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
        environment = dict(os.environ)
        environment.update(
            {
                "DATABASE_URL": f"postgresql://chat-test/{id(database)}",
                "LLM_ENDPOINT": "",
                "LLM_MODEL": "",
                "LLM_API_KEY": "",
            }
        )

        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "streamlit.runtime.secrets.Secrets.load_if_toml_exists",
                return_value=False,
            ),
            patch("db.Database", return_value=database),
            patch("streamlit.dialog", new=inline_dialog),
            patch("streamlit.rerun", side_effect=record_rerun),
        ):
            app = AppTest.from_file(app_path)
            app.session_state["auth"] = {
                "case_id": "CASE-CHAT",
                "role": "A",
            }
            app.session_state["case_tab"] = "③ 调解室"
            app.run(timeout=15)
            app.chat_input[0].set_value("message after stale cache").run(
                timeout=15
            )

        visible = "\n".join(str(item.value) for item in app.markdown)
        self.assertIn("message after stale cache", visible)
        self.assertEqual(database.write_calls, 1)
        self.assertEqual(database.snapshot_views.count("mediation"), 3)
        self.assertEqual(rerun_scopes, [])

    def test_dual_clients_alternate_poll_and_keep_state_actions_available(self):
        database = ChatDatabase(
            status="MAP_READY",
            initial_message_ids=(),
            inserted_message_ids=(41, 57, 88),
        )
        app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
        environment = dict(os.environ)
        environment.update(
            {
                "DATABASE_URL": f"postgresql://chat-test/{id(database)}",
                "LLM_ENDPOINT": "",
                "LLM_MODEL": "",
                "LLM_API_KEY": "",
            }
        )

        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "streamlit.runtime.secrets.Secrets.load_if_toml_exists",
                return_value=False,
            ),
            patch("db.Database", return_value=database),
            patch("streamlit.dialog", new=inline_dialog),
        ):
            app_a = AppTest.from_file(app_path)
            app_a.session_state["auth"] = {
                "case_id": "CASE-CHAT",
                "role": "A",
            }
            app_a.session_state["case_tab"] = "③ 调解室"
            app_a.run(timeout=15)

            app_b = AppTest.from_file(app_path)
            app_b.session_state["auth"] = {
                "case_id": "CASE-CHAT",
                "role": "B",
            }
            app_b.session_state["case_tab"] = "③ 调解室"
            app_b.run(timeout=15)

            app_a.chat_input[0].set_value("message 1 from A").run(timeout=15)
            self.assertFalse(app_a.chat_input[0].disabled)

            b_revision_key = "_case_revision_CASE-CHAT_B"
            self.assertNotEqual(
                database.get_case_revision("CASE-CHAT", "B"),
                app_b.session_state[b_revision_key],
            )
            app_b.run(timeout=15)
            visible_b = "\n".join(
                str(item.value) for item in app_b.markdown
            )
            self.assertIn("message 1 from A", visible_b)

            app_b.chat_input[0].set_value("message 2 from B").run(timeout=15)
            self.assertFalse(app_b.chat_input[0].disabled)

            a_revision_key = "_case_revision_CASE-CHAT_A"
            self.assertNotEqual(
                database.get_case_revision("CASE-CHAT", "A"),
                app_a.session_state[a_revision_key],
            )
            app_a.run(timeout=15)
            visible_a = "\n".join(
                str(item.value) for item in app_a.markdown
            )
            self.assertIn("message 2 from B", visible_a)

            app_a.chat_input[0].set_value("message 3 from A").run(timeout=15)
            self.assertFalse(app_a.chat_input[0].disabled)

            app_b.run(timeout=15)
            visible_b = "\n".join(
                str(item.value) for item in app_b.markdown
            )
            self.assertIn("message 3 from A", visible_b)

            find(app_a.button, "请求暂停").click()
            app_a.run(timeout=15)
            find(app_a.button, "确认暂停").click()
            app_a.run(timeout=15)
            self.assertEqual(database.status, "PAUSED")
            self.assertEqual(database.paused_by, "A")

            find(app_a.button, "我准备好了，恢复调解").click()
            app_a.run(timeout=15)
            find(app_a.button, "确认恢复").click()
            app_a.run(timeout=15)

        self.assertEqual(database.write_calls, 3)
        self.assertEqual(database.pause_calls, 1)
        self.assertEqual(database.resume_calls, 1)
        self.assertEqual(database.status, "MEDIATING")
        self.assertEqual(
            [message["content"] for message in database.messages],
            [
                "message 1 from A",
                "message 2 from B",
                "message 3 from A",
            ],
        )


if __name__ == "__main__":
    unittest.main()
