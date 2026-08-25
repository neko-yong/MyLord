import os
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


TAB_LABELS = {
    "① 独立陈述",
    "② 争议地图",
    "③ 调解室",
    "④ 最终仲裁",
}


def inline_dialog(*_args, **_kwargs):
    return lambda function: function


class FakeDatabase:
    def __init__(self, status, requester=None, role="A"):
        self.status = status
        self.requester = requester
        self.role = role
        self.confirm_calls = 0
        self.request_calls = 0
        self.notifications = []

    def init_db(self):
        return None

    def close(self):
        return None

    def get_case(self, _case_id):
        return {
            "case_id": "CASE-UI",
            "title": "UI evidence freeze test",
            "status": self.status,
            "paused_by": None,
            "arbitration_requested_by": self.requester,
            "arbitration_requested_at": "2026-08-25T12:00:00+00:00",
            "arbitration_started_at": (
                "2026-08-25T12:01:00+00:00"
                if self.status in {"ARBITRATING", "CLOSED"}
                else None
            ),
        }

    def get_case_overview(self, case_id):
        return {
            "case": self.get_case(case_id),
            "submitted": {"A": True, "B": True},
        }

    def get_statement(self, _case_id, role):
        return {"content": f"{role} statement"}

    def get_artifact(self, _case_id, kind):
        if kind == "DISPUTE_MAP":
            return {"id": 1, "content": "dispute map", "evidence_hash": None}
        if kind == "FINAL_JUDGMENT" and self.status == "CLOSED":
            return {
                "id": 2,
                "content": "# Final UI judgment",
                "evidence_hash": "a" * 64,
            }
        return None

    def get_arbitration_evidence(self, _case_id):
        if self.status not in {"ARBITRATING", "CLOSED"}:
            return None
        return {
            "evidence_hash": "a" * 64,
            "snapshot": {"created_at": "2026-08-25T12:01:00+00:00"},
        }

    def get_mediation_snapshot(self, _case_id, _last_message_id=0):
        return {
            "case": self.get_case("CASE-UI"),
            "artifact": {"id": 1, "content": "dispute map"},
            "messages": [],
        }

    def request_arbitration(self, _case_id, role):
        self.request_calls += 1
        self.status = "ARBITRATION_PENDING"
        self.requester = role
        return self.get_case("CASE-UI")

    def cancel_arbitration_request(self, _case_id, _role):
        requester = self.requester
        self.status = "MEDIATING"
        self.requester = None
        if requester and _role != requester:
            self.notifications.append(
                {
                    "id": len(self.notifications) + 1,
                    "event_type": "ARBITRATION_DECLINED",
                    "recipient_role": requester,
                    "actor_role": _role,
                    "read_at": None,
                }
            )
        return self.get_case("CASE-UI")

    def confirm_arbitration(self, _case_id, role):
        if role == self.requester:
            raise AssertionError("requester must not confirm")
        self.confirm_calls += 1
        self.status = "ARBITRATING"
        self.notifications.append(
            {
                "id": len(self.notifications) + 1,
                "event_type": "ARBITRATION_ACCEPTED",
                "recipient_role": self.requester,
                "actor_role": role,
                "read_at": None,
            }
        )
        return self.get_arbitration_evidence("CASE-UI")

    def claim_artifact(self, _case_id, _kind):
        return None

    def get_unread_notifications(self, _case_id, _role):
        return [
            notification
            for notification in self.notifications
            if notification["recipient_role"] == _role
            and notification["read_at"] is None
        ]

    def mark_notification_read(self, _case_id, notification_id, role):
        for notification in self.notifications:
            if (
                notification["id"] == notification_id
                and notification["recipient_role"] == role
                and notification["read_at"] is None
            ):
                notification["read_at"] = "now"
                return True
        return False


def select_tab(app, label):
    tab_key = next(
        key
        for key in app.session_state._state._keys()
        if isinstance(app.session_state[key], str)
        and app.session_state[key] in TAB_LABELS
    )
    app.session_state[tab_key] = label
    return app.run(timeout=15)


def visible_text(elements):
    return "\n".join(str(element.value) for element in elements)


class ArbitrationUITests(unittest.TestCase):
    def run_app(self, database, role):
        app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
        environment = dict(os.environ)
        environment.update(
            {
                "DATABASE_URL": f"postgresql://ui-test/{id(database)}",
                "LLM_ENDPOINT": "https://provider.example/chat/completions",
                "LLM_MODEL": "test-model",
                "LLM_API_KEY": "ui-test-key",
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
        return app_path, patches

    def test_requester_can_request_and_cancel(self):
        database = FakeDatabase("MAP_READY", role="A")
        app_path, patches = self.run_app(database, "A")
        with patches[0], patches[1], patches[2], patches[3]:
            app = AppTest.from_file(app_path)
            app.session_state["auth"] = {"case_id": "CASE-UI", "role": "A"}
            app.run(timeout=15)
            select_tab(app, "④ 最终仲裁")
            self.assertIn(
                "最终仲裁期间不能继续发送消息或请法官介入",
                visible_text(app.info),
            )
            request = next(
                button
                for button in app.button
                if button.label == "申请进入最终仲裁"
            )
            request.click()
            select_tab(app, "④ 最终仲裁")
            self.assertEqual(database.request_calls, 0)
            next(
                button for button in app.button if button.label == "取消"
            ).click()
            app.run(timeout=15)
            self.assertEqual(database.request_calls, 0)
            select_tab(app, "④ 最终仲裁")
            next(
                button
                for button in app.button
                if button.label == "申请进入最终仲裁"
            ).click()
            select_tab(app, "④ 最终仲裁")
            next(
                button for button in app.button if button.label == "确认申请"
            ).click()
            app.run(timeout=15)
            select_tab(app, "④ 最终仲裁")

            self.assertEqual(database.request_calls, 1)
            self.assertTrue(
                any(
                    button.label == "取消最终仲裁申请"
                    for button in app.button
                )
            )
            self.assertIn("等待 B 确认", visible_text(app.warning))
            self.assertIn("已申请进入最终仲裁", visible_text(app.warning))

            cancel = next(
                button
                for button in app.button
                if button.label == "取消最终仲裁申请"
            )
            cancel.click()
            select_tab(app, "④ 最终仲裁")
            self.assertEqual(database.status, "MEDIATING")
            self.assertTrue(
                any(
                    button.label == "申请进入最终仲裁"
                    for button in app.button
                )
            )

    def test_other_party_can_continue_mediation(self):
        database = FakeDatabase(
            "ARBITRATION_PENDING",
            requester="A",
            role="B",
        )
        app_path, patches = self.run_app(database, "B")
        with patches[0], patches[1], patches[2], patches[3]:
            app = AppTest.from_file(app_path)
            app.session_state["auth"] = {"case_id": "CASE-UI", "role": "B"}
            app.run(timeout=15)
            select_tab(app, "④ 最终仲裁")
            self.assertFalse(
                any(
                    button.label == "申请进入最终仲裁"
                    for button in app.button
                )
            )

            continue_button = next(
                button for button in app.button if button.label == "继续调解"
            )
            continue_button.click()
            select_tab(app, "④ 最终仲裁")
            self.assertEqual(database.status, "ARBITRATION_PENDING")
            next(
                button for button in app.button if button.label == "取消"
            ).click()
            app.run(timeout=15)
            self.assertEqual(database.status, "ARBITRATION_PENDING")
            select_tab(app, "④ 最终仲裁")
            next(
                button for button in app.button if button.label == "继续调解"
            ).click()
            select_tab(app, "④ 最终仲裁")
            next(
                button
                for button in app.button
                if button.label == "确认继续调解"
            ).click()
            app.run(timeout=15)
            select_tab(app, "④ 最终仲裁")

            self.assertEqual(database.status, "MEDIATING")
            self.assertTrue(
                any(
                    button.label == "申请进入最终仲裁"
                    for button in app.button
                )
            )

    def test_other_party_can_decline_or_confirm_and_lock_ui(self):
        database = FakeDatabase(
            "ARBITRATION_PENDING",
            requester="A",
            role="B",
        )
        app_path, patches = self.run_app(database, "B")
        with patches[0], patches[1], patches[2], patches[3]:
            app = AppTest.from_file(app_path)
            app.session_state["auth"] = {"case_id": "CASE-UI", "role": "B"}
            app.run(timeout=15)
            select_tab(app, "④ 最终仲裁")
            labels = {button.label for button in app.button}
            self.assertIn("继续调解", labels)
            self.assertIn("同意进入最终仲裁", labels)

            confirm = next(
                button
                for button in app.button
                if button.label == "同意进入最终仲裁"
            )
            confirm.click()
            select_tab(app, "④ 最终仲裁")
            self.assertEqual(database.confirm_calls, 0)
            next(
                button for button in app.button if button.label == "返回"
            ).click()
            app.run(timeout=15)
            self.assertEqual(database.confirm_calls, 0)
            self.assertEqual(database.status, "ARBITRATION_PENDING")
            select_tab(app, "④ 最终仲裁")
            next(
                button
                for button in app.button
                if button.label == "同意进入最终仲裁"
            ).click()
            select_tab(app, "④ 最终仲裁")
            next(
                button
                for button in app.button
                if button.label == "确认并冻结证据"
            ).click()
            app.run(timeout=15)
            select_tab(app, "④ 最终仲裁")
            self.assertEqual(database.confirm_calls, 1)
            select_tab(app, "④ 最终仲裁")
            self.assertIn("证据已冻结", visible_text(app.warning))

            select_tab(app, "③ 调解室")
            self.assertEqual(len(app.chat_input), 0)
            self.assertFalse(
                any(button.label == "请法官介入" for button in app.button)
            )
            self.assertFalse(
                any(button.label == "请求暂停" for button in app.button)
            )
            self.assertFalse(
                any(
                    button.label == "我准备好了，恢复调解"
                    for button in app.button
                )
            )
            self.assertIn("证据已冻结", visible_text(app.warning))

    def test_closed_case_only_displays_final_judgment(self):
        database = FakeDatabase("CLOSED", requester="A", role="A")
        app_path, patches = self.run_app(database, "A")
        with patches[0], patches[1], patches[2], patches[3]:
            app = AppTest.from_file(app_path)
            app.session_state["auth"] = {"case_id": "CASE-UI", "role": "A"}
            app.run(timeout=15)
            select_tab(app, "④ 最终仲裁")

            self.assertIn("Final UI judgment", visible_text(app.markdown))
            self.assertFalse(
                any(
                    button.label == "申请进入最终仲裁"
                    for button in app.button
                )
            )

    def test_persistent_notification_is_read_only_after_acknowledgement(self):
        expected = {
            "ARBITRATION_DECLINED": "B 选择继续调解",
            "ARBITRATION_ACCEPTED": "B 已同意进入最终仲裁",
        }
        for event_type, message in expected.items():
            with self.subTest(event_type=event_type):
                database = FakeDatabase("MEDIATING", role="A")
                database.notifications.append(
                    {
                        "id": 1,
                        "event_type": event_type,
                        "recipient_role": "A",
                        "actor_role": "B",
                        "read_at": None,
                    }
                )
                app_path, patches = self.run_app(database, "A")
                with patches[0], patches[1], patches[2], patches[3]:
                    app = AppTest.from_file(app_path)
                    app.session_state["auth"] = {
                        "case_id": "CASE-UI",
                        "role": "A",
                    }
                    app.run(timeout=15)
                    self.assertIn(message, visible_text(app.markdown))
                    self.assertIsNone(database.notifications[0]["read_at"])
                    next(
                        button
                        for button in app.button
                        if button.label == "知道了"
                    ).click()
                    app.run(timeout=15)
                    self.assertEqual(database.notifications[0]["read_at"], "now")
                    self.assertNotIn(message, visible_text(app.markdown))

    def test_closed_result_appears_on_next_normal_rerun(self):
        database = FakeDatabase("ARBITRATING", requester="A", role="A")
        app_path, patches = self.run_app(database, "A")
        with patches[0], patches[1], patches[2], patches[3]:
            app = AppTest.from_file(app_path)
            app.session_state["auth"] = {"case_id": "CASE-UI", "role": "A"}
            app.run(timeout=15)
            select_tab(app, "④ 最终仲裁")
            self.assertIn("最终仲裁正在进行", visible_text(app.warning))

            database.status = "CLOSED"
            select_tab(app, "④ 最终仲裁")

            self.assertIn("Final UI judgment", visible_text(app.markdown))


if __name__ == "__main__":
    unittest.main()
