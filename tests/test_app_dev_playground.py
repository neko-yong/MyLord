import os
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.test_dev_tools import MemoryDatabase
from tests.test_app_arbitration_ui import select_tab


class AppMemoryDatabase(MemoryDatabase):
    def init_db(self):
        return None

    def close(self):
        return None

    def authenticate(self, _case_id, _token):
        return None

    def get_case_overview(self, case_id):
        return {
            "case": self.get_case(case_id),
            "submitted": self.get_submission_status(case_id),
        }

    def get_statement(self, case_id, role):
        content = self.statements.get((case_id, role))
        return {"content": content} if content else None


def app_environment(database=None, dev_mode=True):
    app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
    environment = dict(os.environ)
    environment.update(
        {
            "DATABASE_URL": (
                (
                    f"postgresql://dev-ui-test/{id(database)}"
                    if database is not None
                    else ""
                )
                if dev_mode
                else f"postgresql://dev-ui-test/{id(database)}"
            ),
            "DEV_MODE": "true" if dev_mode else "false",
            "LLM_MODE": "mock",
            "DEV_DATABASE_MODE": "local",
        }
    )
    database_patch = (
        patch("db.Database", side_effect=AssertionError("PostgreSQL called"))
        if dev_mode and database is None
        else patch("db.Database", return_value=database)
    )
    return app_path, (
        patch.dict(os.environ, environment, clear=True),
        patch(
            "streamlit.runtime.secrets.Secrets.load_if_toml_exists",
            return_value=False,
        ),
        database_patch,
        patch("llm.call_llm", side_effect=AssertionError("Real LLM called")),
    )


def find(items, label):
    return next(item for item in items if item.label == label)


def all_visible_text(app):
    collections = (
        app.title,
        app.header,
        app.subheader,
        app.markdown,
        app.caption,
        app.info,
        app.warning,
        app.error,
        app.expander,
        app.button,
        app.selectbox,
        app.segmented_control,
    )
    return "\n".join(
        str(getattr(element, "value", getattr(element, "label", "")))
        + " "
        + str(getattr(element, "label", ""))
        for collection in collections
        for element in collection
    )


class DeveloperPlaygroundAppTests(unittest.TestCase):
    def test_dev_ui_create_role_switch_debug_and_delete(self):
        app_path, patches = app_environment(dev_mode=True)

        with (
            patches[0],
            patches[1],
            patches[2] as postgres,
            patches[3] as real_llm,
        ):
            app = AppTest.from_file(app_path).run(timeout=15)
            text = all_visible_text(app)
            self.assertIn("Developer Playground", text)
            self.assertIsNotNone(find(app.selectbox, "Fixture"))
            self.assertIsNotNone(find(app.selectbox, "Scenario"))
            self.assertEqual(find(app.selectbox, "LLM Mode").value, "mock")
            self.assertEqual(
                find(app.segmented_control, "Database Mode").value,
                "local",
            )
            self.assertIn("Fast Local", text)

            find(app.button, "创建测试案件").click()
            app.run(timeout=15)

            dev_case = app.session_state["dev_case"]
            self.assertTrue(dev_case.case_id)
            self.assertEqual(
                app.session_state["_dev_local_store"]["cases"][
                    dev_case.case_id
                ]["status"],
                "MEDIATING",
            )
            text = all_visible_text(app)
            self.assertIn("Evidence Snapshot", text)
            self.assertIn("Mock Calls", text)
            self.assertNotIn(dev_case.a_token, text)
            self.assertNotIn(dev_case.b_token, text)

            role_switch = find(app.segmented_control, "查看身份")
            role_switch.set_value("B")
            app.run(timeout=15)
            self.assertEqual(app.session_state["dev_view_role"], "B")
            self.assertIsNone(app.session_state["auth"])

            find(app.button, "删除当前测试案件").click()
            app.run(timeout=15)
            self.assertNotIn(
                dev_case.case_id,
                app.session_state["_dev_local_store"]["cases"],
            )
            self.assertNotIn("dev_case", app.session_state)

            self.assertEqual(postgres.mock_calls, [])
            self.assertEqual(real_llm.mock_calls, [])

    def test_dev_fast_full_workflow_and_reset_use_no_network_backend(self):
        app_path, patches = app_environment(dev_mode=True)
        with patches[0], patches[1], patches[2] as postgres, patches[3] as real_llm:
            app = AppTest.from_file(app_path).run(timeout=15)
            find(app.button, "创建测试案件").click()
            app.run(timeout=15)
            dev_case = app.session_state["dev_case"]

            select_tab(app, "④ 最终仲裁")
            find(app.button, "申请进入最终仲裁").click()
            select_tab(app, "④ 最终仲裁")
            find(app.button, "取消最终仲裁申请").click()
            select_tab(app, "④ 最终仲裁")
            find(app.button, "申请进入最终仲裁").click()
            select_tab(app, "④ 最终仲裁")

            find(app.segmented_control, "查看身份").set_value("B")
            app.run(timeout=15)
            self.assertEqual(app.session_state["dev_view_role"], "B")
            select_tab(app, "④ 最终仲裁")
            find(app.button, "同意进入最终仲裁").click()
            select_tab(app, "④ 最终仲裁")

            store = app.session_state["_dev_local_store"]
            self.assertEqual(store["cases"][dev_case.case_id]["status"], "CLOSED")
            self.assertIn(
                (dev_case.case_id, "ARBITRATION_EVIDENCE"),
                store["artifacts"],
            )
            for kind in (
                "JUDGMENT_NORMAL",
                "JUDGMENT_SWAPPED",
                "META_JUDGMENT",
                "FINAL_JUDGMENT",
            ):
                self.assertIn((dev_case.case_id, kind), store["artifacts"])
            self.assertEqual(postgres.mock_calls, [])
            self.assertEqual(real_llm.mock_calls, [])

            find(app.button, "Reset Local Playground").click()
            app.run(timeout=15)
            self.assertEqual(app.session_state["_dev_local_store"]["cases"], {})
            self.assertNotIn("dev_case", app.session_state)

    def test_production_hides_all_developer_controls(self):
        database = AppMemoryDatabase()
        app_path, patches = app_environment(database, False)

        with patches[0], patches[1], patches[2], patches[3]:
            app = AppTest.from_file(app_path).run(timeout=15)

        text = all_visible_text(app)
        for forbidden in (
            "Developer Playground",
            "Fixture",
            "Scenario",
            "LLM Mode",
            "模拟失败阶段",
            "查看身份",
            "创建测试案件",
            "Database Mode",
            "Fast Local",
            "Reset Local Playground",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_switching_database_mode_clears_active_case_reference(self):
        postgres_database = AppMemoryDatabase()
        app_path, patches = app_environment(postgres_database, True)
        with patches[0], patches[1], patches[2] as postgres, patches[3]:
            app = AppTest.from_file(app_path).run(timeout=15)
            find(app.button, "创建测试案件").click()
            app.run(timeout=15)
            local_case = app.session_state["dev_case"].case_id

            find(app.segmented_control, "Database Mode").set_value("postgres")
            app.run(timeout=15)

            self.assertNotIn("dev_case", app.session_state)
            self.assertIn(
                local_case,
                app.session_state["_dev_local_store"]["cases"],
            )
            self.assertEqual(postgres.call_count, 1)
            self.assertIn("需要创建新的测试案件", all_visible_text(app))


if __name__ == "__main__":
    unittest.main()
