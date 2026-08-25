import os
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class MissingDatabaseUITests(unittest.TestCase):
    def test_missing_database_url_shows_friendly_error(self):
        app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
        clean_environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "DATABASE_URL",
                "LLM_ENDPOINT",
                "LLM_MODEL",
                "LLM_API_KEY",
                "ADMIN_CREATE_SECRET",
            }
        }

        with (
            patch.dict(os.environ, clean_environment, clear=True),
            patch(
                "streamlit.runtime.secrets.Secrets.load_if_toml_exists",
                return_value=False,
            ),
        ):
            app = AppTest.from_file(app_path).run(timeout=15)

        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            any("数据库尚未配置" in element.value for element in app.error)
        )


if __name__ == "__main__":
    unittest.main()
