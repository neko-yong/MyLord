import json
import os
import unittest
from functools import wraps
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.test_app_arbitration_ui import FakeDatabase as ArbitrationDatabase
from tests.test_app_chat_fragment import ChatDatabase
from tests.test_app_statement_validation import (
    FakeStatementDatabase,
    fill_valid_required_fields,
)


def inline_dialog(*_args, **_kwargs):
    return lambda function: function


def find(items, label):
    return next(item for item in items if item.label == label)


class QueuedRevisionDatabase:
    """Add one synthetic external revision after a committed confirmation."""

    def __init__(self, database, committed):
        self.database = database
        self.committed = committed
        self.epoch = 0
        self.injected = 0

    def __getattr__(self, name):
        return getattr(self.database, name)

    def get_case_revision(self, *args, **kwargs):
        revision = dict(self.database.get_case_revision(*args, **kwargs))
        revision["test_external_epoch"] = self.epoch
        return revision

    def get_case_view_snapshot(self, *args, **kwargs):
        snapshot = self.database.get_case_view_snapshot(*args, **kwargs)
        snapshot["revision"] = dict(snapshot["revision"])
        snapshot["revision"]["test_external_epoch"] = self.epoch
        return snapshot

    def inject_once_after_commit(self):
        if self.injected == 0 and self.committed():
            self.epoch += 1
            self.injected += 1


def queued_sync_fragment(database):
    """Reliably emulate a queued sync fragment immediately after the full-run skip."""

    def fragment(function=None, *_args, **_kwargs):
        def decorate(target):
            if target.__name__ != "live_case_sync":
                return target

            @wraps(target)
            def run_with_queued_sync(*args, **kwargs):
                target(*args, **kwargs)
                database.inject_once_after_commit()
                return target(*args, **kwargs)

            return run_with_queued_sync

        return decorate(function) if callable(function) else decorate

    return fragment


def state_rows(lines):
    return [
        json.loads(line.split("STATE ", 1)[1])
        for line in lines
        if "STATE " in line
    ]


class ConfirmationRerunOrderTests(unittest.TestCase):
    APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")

    def run_path(self, path):
        if path == "statement":
            raw = FakeStatementDatabase()
            committed = lambda: "A" in raw.saved
            role, tab = "A", "① 独立陈述"
        elif path == "pause":
            raw = ChatDatabase(status="MEDIATING")
            committed = lambda: raw.pause_calls == 1
            role, tab = "A", "③ 调解室"
        else:
            raw = ArbitrationDatabase(
                "ARBITRATION_PENDING",
                requester="A",
                role="B",
            )
            committed = lambda: raw.confirm_calls == 1
            role, tab = "B", "④ 最终仲裁"
        database = QueuedRevisionDatabase(raw, committed)
        environment = dict(os.environ)
        environment.update({
            "DATABASE_URL": f"postgresql://queued-sync-fixture/{id(database)}",
            "LLM_ENDPOINT": "https://fixture.invalid/v1",
            "LLM_MODEL": "fixture-model",
            "LLM_API_KEY": "fixture-key",
            "DEVELOPMENT_MODE": "true",
            "DEV_MODE": "false",
            "RERUN_STATE_TRACE": "true",
        })
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "streamlit.runtime.secrets.Secrets.load_if_toml_exists",
                return_value=False,
            ),
            patch("db.Database", return_value=database),
            patch("streamlit.dialog", new=inline_dialog),
            patch("streamlit.fragment", new=queued_sync_fragment(database)),
            self.assertLogs("state_trace", level="WARNING") as captured,
        ):
            app = AppTest.from_file(self.APP_PATH)
            app.session_state["auth"] = {"case_id": "CASE-UI", "role": role}
            app.session_state["case_tab"] = tab
            app.run(timeout=15)
            if path == "statement":
                fill_valid_required_fields(app)
                find(app.button, "提交并冻结").click().run(timeout=15)
                find(app.button, "确认提交").click().run(timeout=15)
            elif path == "pause":
                find(app.button, "请求暂停").click().run(timeout=15)
                find(app.button, "确认暂停").click().run(timeout=15)
            else:
                find(app.button, "同意进入最终仲裁").click().run(timeout=15)
                find(app.button, "确认并冻结证据").click().run(timeout=15)

        self.assertFalse(app.exception)
        self.assertEqual(database.injected, 1)
        self.assertNotIn("_pending_confirmation", app.session_state)
        self.assertEqual(len(app.tabs), 4)
        return raw, state_rows(captured.output)

    def test_tabs_and_selected_content_exist_before_queued_sync_requests_rerun(self):
        for path in ("statement", "pause", "arbitration_accept"):
            with self.subTest(path=path):
                raw, rows = self.run_path(path)
                action_events = [
                    row["event"]
                    for row in rows
                    if row["confirmation_action"] == path
                ]
                for event in (
                    "confirmation_received",
                    "action_persisted",
                    "pending_cleared",
                    "full_rerun_requested",
                ):
                    self.assertIn(event, action_events)
                self.assertLess(
                    action_events.index("confirmation_received"),
                    action_events.index("action_persisted"),
                )
                self.assertLess(
                    action_events.index("action_persisted"),
                    action_events.index("pending_cleared"),
                )
                self.assertLess(
                    action_events.index("pending_cleared"),
                    action_events.index("full_rerun_requested"),
                )
                run_sequences = [
                    row["run_sequence"]
                    for row in rows
                    if row["event"] == "new_run"
                ]
                self.assertTrue(all(sequence > 0 for sequence in run_sequences))
                self.assertEqual(run_sequences, sorted(set(run_sequences)))
                rerun_sequence = next(
                    row["run_sequence"]
                    for row in rows
                    if row["event"] == "live_sync_rerun_decision"
                    and row["phase_outcome"] == "rerun"
                )
                events = [
                    row["event"]
                    for row in rows
                    if row["run_sequence"] == rerun_sequence
                ]
                self.assertIn("tabs_register_after", events)
                self.assertIn("render_complete", events)
                self.assertLess(
                    events.index("tabs_register_after"),
                    events.index("live_sync_rerun_decision"),
                )
                self.assertLess(
                    events.index("render_complete"),
                    events.index("live_sync_rerun_decision"),
                )
                if path == "statement":
                    self.assertEqual(set(raw.saved), {"A"})
                elif path == "pause":
                    self.assertEqual(raw.pause_calls, 1)
                else:
                    self.assertEqual(raw.confirm_calls, 1)


if __name__ == "__main__":
    unittest.main()
