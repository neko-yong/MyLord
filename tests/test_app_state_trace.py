import os
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from db import DatabaseUnavailable
from tests.test_app_arbitration_ui import FakeDatabase


class AppStateTraceTests(unittest.TestCase):
    def run_snapshot_case(self, database, enabled):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
        app.session_state["auth"] = {"case_id": "CASE-UI", "role": "A"}
        app.session_state["case_tab"] = "② 争议地图"
        with (
            patch.dict(os.environ, {
                "DATABASE_URL": f"postgresql://trace-fixture/{id(database)}",
                "LLM_ENDPOINT": "", "LLM_MODEL": "", "LLM_API_KEY": "",
                "RERUN_STATE_TRACE": "true" if enabled else "false",
            }),
            patch("streamlit.runtime.secrets.Secrets.load_if_toml_exists", return_value=False),
            patch("db.Database", return_value=database),
        ):
            app.run(timeout=15)
        self.assertFalse(app.exception)
        return app

    def test_database_failure_is_visible_and_preserves_auth_with_trace(self):
        database = FakeDatabase("MEDIATING")
        with (
            patch.object(database, "get_case_view_snapshot", side_effect=DatabaseUnavailable("Test outage")),
            self.assertLogs("state_trace", level="WARNING") as captured,
        ):
            app = self.run_snapshot_case(database, True)
        self.assertTrue(app.error)
        self.assertEqual(app.session_state["auth"], {"case_id": "CASE-UI", "role": "A"})
        self.assertIn('"event": "snapshot_failed"', "\n".join(captured.output))

    def test_missing_case_retains_existing_auth_invalidation_contract(self):
        database = FakeDatabase("MEDIATING")
        with (
            patch.object(database, "get_case_view_snapshot", return_value=None),
            self.assertLogs("state_trace", level="WARNING") as captured,
        ):
            app = self.run_snapshot_case(database, True)
        self.assertTrue(app.error)
        self.assertIsNone(app.session_state["auth"])
        stop = next(line for line in captured.output if '"event": "stop"' in line)
        self.assertIn('"authenticated": false', stop)

    def test_normal_render_and_default_off(self):
        with self.assertNoLogs("state_trace", level="WARNING"):
            app = self.run_snapshot_case(FakeDatabase("MEDIATING"), False)
        self.assertIn("### 争议地图", [item.value for item in app.markdown])
        with self.assertLogs("state_trace", level="WARNING") as captured:
            traced_app = self.run_snapshot_case(FakeDatabase("MEDIATING"), True)
        self.assertIn("### 争议地图", [item.value for item in traced_app.markdown])
        completed = next(line for line in captured.output if '"event": "render_complete"' in line)
        self.assertIn('"selected_tab_open_flags": "0100"', completed)
        self.assertIn('"render_branch": "dispute"', completed)


if __name__ == "__main__":
    unittest.main()
