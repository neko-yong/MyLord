import os
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class FakeStatementDatabase:
    def __init__(self):
        self.saved = {}

    def init_db(self):
        return None

    def close(self):
        return None

    def get_case(self, _case_id):
        return {
            "case_id": "CASE-STATEMENT-UI",
            "title": "Statement validation UI test",
            "status": "COLLECTING",
            "paused_by": None,
            "arbitration_requested_by": None,
            "arbitration_requested_at": None,
            "arbitration_started_at": None,
        }

    def get_case_overview(self, case_id):
        return {
            "case": self.get_case(case_id),
            "submitted": {
                "A": "A" in self.saved,
                "B": "B" in self.saved,
            },
        }

    def get_statement(self, _case_id, role):
        content = self.saved.get(role)
        return {"content": content} if content else None

    def save_statement(self, _case_id, role, content):
        self.saved[role] = content


def app_environment(database):
    app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
    environment = dict(os.environ)
    environment.update(
        {
            "DATABASE_URL": f"postgresql://statement-ui-test/{id(database)}",
            "LLM_ENDPOINT": "https://provider.example/chat/completions",
            "LLM_MODEL": "test-model",
            "LLM_API_KEY": "ui-test-key",
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


def text_area(app, prefix):
    return next(area for area in app.text_area if area.label.startswith(prefix))


def submit_statement(app):
    button = next(button for button in app.button if button.label == "提交并冻结")
    button.click()
    return app.run(timeout=15)


def fill_valid_required_fields(app, *, omit=None):
    values = {
        "1.": "事情从周末安排的讨论开始，后来双方发生了争执。",
        "3.": "对方临时取消约定，而且没有提前说明。",
        "4.": "我提高了声音，并且打断了对方说话。",
        "6.": "提前告知",
        "7.": "希望以后改动共同安排前先沟通。",
    }
    for prefix, value in values.items():
        if prefix != omit:
            text_area(app, prefix).set_value(value)


class StatementValidationUITests(unittest.TestCase):
    def run_app(self, database):
        app_path, patches = app_environment(database)
        app = AppTest.from_file(app_path)
        app.session_state["auth"] = {
            "case_id": "CASE-STATEMENT-UI",
            "role": "A",
        }
        return app, patches

    def test_empty_submission_shows_error_and_does_not_save(self):
        database = FakeStatementDatabase()
        app, patches = self.run_app(database)

        with patches[0], patches[1], patches[2]:
            app.run(timeout=15)
            labels = {area.label for area in app.text_area}
            self.assertTrue(
                {
                    "1. 事情是怎么开始 / 发生的？（必填）",
                    "2. 关键时间线（选填）",
                    "3. 对方哪些具体行为让你不满？（必填）",
                    "4. 你当时具体做了什么？（必填）",
                    "5. 当时的情绪（选填）",
                    "6. 你真正需要 / 在意的是什么？（必填）",
                    "7. 你希望对方做什么 / 希望这次解决什么？（必填）",
                    "8. 你认为自己可能哪里做得不好？（选填）",
                    "9. 原话 / 聊天记录 / 其他补充（选填）",
                }.issubset(labels)
            )
            submit_statement(app)

        errors = "\n".join(str(element.value) for element in app.error)
        self.assertIn("还有必填内容没有完成", errors)
        self.assertIn("事情经过", errors)
        self.assertEqual(database.saved, {})

    def test_partial_submission_names_missing_field_and_does_not_save(self):
        database = FakeStatementDatabase()
        app, patches = self.run_app(database)

        with patches[0], patches[1], patches[2]:
            app.run(timeout=15)
            fill_valid_required_fields(app, omit="4.")
            submit_statement(app)

        errors = "\n".join(str(element.value) for element in app.error)
        self.assertIn("你当时具体做了什么", errors)
        self.assertEqual(database.saved, {})
        self.assertEqual(
            text_area(app, "1.").value,
            "事情从周末安排的讨论开始，后来双方发生了争执。",
        )

    def test_valid_required_fields_submit_and_freeze_optional_placeholders(self):
        database = FakeStatementDatabase()
        app, patches = self.run_app(database)

        with patches[0], patches[1], patches[2]:
            app.run(timeout=15)
            fill_valid_required_fields(app)
            submit_statement(app)

        self.assertIn("A", database.saved)
        self.assertEqual(database.saved["A"].count("（未提供）"), 4)
        successes = "\n".join(str(element.value) for element in app.success)
        self.assertIn("已经提交", successes)
        self.assertIn("当前版本已冻结", successes)


if __name__ == "__main__":
    unittest.main()
