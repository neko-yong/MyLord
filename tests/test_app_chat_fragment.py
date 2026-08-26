import os
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


def inline_dialog(*_args, **_kwargs):
    return lambda function: function


class ChatDatabase:
    def __init__(self):
        self.snapshot_views = []
        self.write_calls = 0
        self.messages = [
            {
                "id": 1,
                "case_id": "CASE-CHAT",
                "sender": "B",
                "content": "existing message",
                "created_at": "before",
            }
        ]
        self.updated_at = "before"

    def init_db(self):
        return None

    def close(self):
        return None

    def get_case_revision(self, _case_id, _role):
        return {
            "status": "MEDIATING",
            "updated_at": self.updated_at,
            "latest_message_id": self.messages[-1]["id"],
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
                "status": "MEDIATING",
                "paused_by": None,
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
        self.updated_at = "after"
        message = {
            "id": self.messages[-1]["id"] + 1,
            "case_id": case_id,
            "sender": sender,
            "content": content,
            "created_at": "after",
            "case_status": "MEDIATING",
            "case_updated_at": self.updated_at,
        }
        self.messages.append(message)
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


if __name__ == "__main__":
    unittest.main()
