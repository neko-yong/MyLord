import ast
import os
import unittest
from unittest.mock import patch

import streamlit as st

from tests import test_admin_console as admin_tests
from tests.test_app_chat_fragment import ChatDatabase


class PaginatedAdminDatabase(admin_tests.FakeAdminDatabase):
    def __init__(self):
        super().__init__()
        self.rows = [dict(self.metadata)]
        for index in range(25):
            row = dict(self.metadata)
            row["case_id"] = f"CASE-PAGE-{index:03d}"
            self.rows.append(row)

    def list_case_metadata(self, limit, offset):
        self.last_page = (limit, offset)
        return {
            "total": len(self.rows),
            "cases": self.rows[offset : offset + limit],
        }


class AdminLayoutStabilityTests(unittest.TestCase):
    def setUp(self):
        self.harness = admin_tests.AdminConsoleAppTests()
        self.harness.setUp()

    def _run_and_capture(self, app, database, path):
        calls = []
        real_set_page_config = st.set_page_config

        def capture_page_config(*args, **kwargs):
            calls.append(dict(kwargs))
            return real_set_page_config(*args, **kwargs)

        with patch(
            "streamlit.set_page_config",
            side_effect=capture_page_config,
        ):
            self.harness._run(app, database)

        self.assertTrue(calls, f"{path}: page config was not set")
        self.assertEqual(
            [call.get("layout") for call in calls],
            ["wide"] * len(calls),
            f"{path}: conflicting page layouts were emitted",
        )
        self.assertEqual(
            [call.get("initial_sidebar_state") for call in calls],
            ["collapsed"] * len(calls),
            f"{path}: Admin sidebar state was not stable",
        )

    def test_admin_layout_remains_wide_through_every_rerun_path(self):
        database = PaginatedAdminDatabase()
        app = self.harness._new_app(admin_tests.ROUTE_KEY)

        self._run_and_capture(app, database, "initial load")

        admin_tests.find(app.text_input, "Maintenance secret").set_value(
            "wrong-secret"
        )
        admin_tests.find(app.button, "Sign in").click()
        self._run_and_capture(app, database, "wrong login")

        admin_tests.find(app.text_input, "Maintenance secret").set_value(
            admin_tests.MAINTENANCE_SECRET
        )
        admin_tests.find(app.button, "Sign in").click()
        self._run_and_capture(app, database, "correct login")

        self._run_and_capture(app, database, "case list")
        self.assertEqual(database.last_page, (25, 0))

        admin_tests.find(app.text_input, "Full Case ID").set_value(
            admin_tests.CASE_ID
        )
        admin_tests.find(app.button, "Find case").click()
        self._run_and_capture(app, database, "exact search")

        admin_tests.find(app.button, "Permanently delete").click()
        self._run_and_capture(app, database, "delete confirmation")

        admin_tests.find(app.checkbox, "我确认删除这个案件，且无法恢复").check()
        self._run_and_capture(app, database, "delete acknowledgement")

        admin_tests.find(app.button, "Cancel").click()
        self._run_and_capture(app, database, "cancel delete")
        self.assertEqual(database.delete_calls, [])

        admin_tests.find(app.button, "Next").click()
        self._run_and_capture(app, database, "page change")
        self.assertEqual(database.last_page, (25, 25))

        self._run_and_capture(app, database, "refresh")

    def test_normal_route_remains_centered(self):
        scenarios = (
            ("landing", None),
            ("enter case / statement", "① 独立陈述"),
            ("dispute map", "② 争议地图"),
            ("mediation", "③ 调解室"),
            ("arbitration", "④ 最终仲裁"),
        )
        for path, selected_tab in scenarios:
            with self.subTest(path=path):
                app = self.harness._new_app()
                database = None
                if selected_tab is not None:
                    database = ChatDatabase()
                    app.session_state["auth"] = {
                        "case_id": "CASE-CHAT",
                        "role": "A",
                    }
                    app.session_state["case_tab"] = selected_tab
                calls = []
                real_set_page_config = st.set_page_config

                def capture_page_config(*args, **kwargs):
                    calls.append(dict(kwargs))
                    return real_set_page_config(*args, **kwargs)

                with patch(
                    "streamlit.set_page_config",
                    side_effect=capture_page_config,
                ):
                    self.harness._run(app, database)

                self.assertEqual(
                    [call.get("layout") for call in calls],
                    ["centered"] * len(calls),
                )

    def test_page_config_has_one_app_call_site_and_none_in_admin_module(self):
        project_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..")
        )

        def page_config_call_count(path):
            with open(path, encoding="utf-8") as source:
                tree = ast.parse(source.read())
            return sum(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "st"
                and node.func.attr == "set_page_config"
                for node in ast.walk(tree)
            )

        self.assertEqual(
            page_config_call_count(os.path.join(project_root, "app.py")),
            1,
        )
        self.assertEqual(
            page_config_call_count(
                os.path.join(project_root, "admin_console.py")
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
