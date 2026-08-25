import os
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.test_dev_tools import MemoryDatabase


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


def app_environment(database, dev_mode):
    app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
    environment = dict(os.environ)
    environment.update(
        {
            "DATABASE_URL": f"postgresql://dev-ui-test/{id(database)}",
            "DEV_MODE": "true" if dev_mode else "false",
            "LLM_MODE": "mock",
        }
    )
    return app_path, (
        patch.dict(os.environ, environment, clear=True),
        patch(
            "streamlit.runtime.secrets.Secrets.load_if_toml_exists",
            return_value=False,
        ),
        patch("db.Database", return_value=database),
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
        database = AppMemoryDatabase()
        app_path, patches = app_environment(database, True)

        with patches[0], patches[1], patches[2]:
            app = AppTest.from_file(app_path).run(timeout=15)
            text = all_visible_text(app)
            self.assertIn("Developer Playground", text)
            self.assertIsNotNone(find(app.selectbox, "Fixture"))
            self.assertIsNotNone(find(app.selectbox, "Scenario"))
            self.assertEqual(find(app.selectbox, "LLM Mode").value, "mock")

            find(app.button, "创建测试案件").click()
            app.run(timeout=15)

            dev_case = app.session_state["dev_case"]
            self.assertTrue(dev_case.case_id)
            self.assertEqual(
                database.get_case(dev_case.case_id)["status"],
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
            self.assertNotIn(dev_case.case_id, database.cases)
            self.assertNotIn("dev_case", app.session_state)

    def test_production_hides_all_developer_controls(self):
        database = AppMemoryDatabase()
        app_path, patches = app_environment(database, False)

        with patches[0], patches[1], patches[2]:
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
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
