import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from admin_console import is_admin_route


ROUTE_KEY = "ROUTE_KEY_CANARY_92X"
MAINTENANCE_SECRET = "MAINTENANCE_SECRET_CANARY_31K"
CASE_ID = "CASE-ABC123"
PRIVATE_STATEMENT = "PRIVATE_STATEMENT_CANARY_92X"
PRIVATE_MESSAGE = "PRIVATE_MESSAGE_CANARY_31K"
PRIVATE_JUDGMENT = "PRIVATE_JUDGMENT_CANARY_77P"


def inline_dialog(*_args, **_kwargs):
    return lambda function: function


def find(items, label):
    return next(item for item in items if item.label == label)


def visible_text(app):
    collections = (
        app.title,
        app.header,
        app.subheader,
        app.markdown,
        app.caption,
        app.info,
        app.success,
        app.warning,
        app.error,
        app.metric,
        app.dataframe,
        app.table,
        app.button,
        app.text_input,
    )
    return "\n".join(
        f"{getattr(element, 'value', '')} {getattr(element, 'label', '')}"
        for collection in collections
        for element in collection
    )


def app_environment(database=None, include_route=True, include_secret=True):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DATABASE_URL",
            "LLM_ENDPOINT",
            "LLM_MODEL",
            "LLM_API_KEY",
            "ADMIN_CREATE_SECRET",
            "ADMIN_CONSOLE_ROUTE_KEY",
            "ADMIN_MAINTENANCE_SECRET",
            "DEV_MODE",
            "DEV_DATABASE_MODE",
            "LLM_MODE",
        }
    }
    if database is not None:
        environment["DATABASE_URL"] = f"postgresql://admin-ui-test/{id(database)}"
    if include_route:
        environment["ADMIN_CONSOLE_ROUTE_KEY"] = ROUTE_KEY
    if include_secret:
        environment["ADMIN_MAINTENANCE_SECRET"] = MAINTENANCE_SECRET
    environment["DEV_MODE"] = "false"
    return environment


class FakeAdminDatabase:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.metadata = {
            "case_id": CASE_ID,
            "status": "MEDIATING",
            "created_at": now,
            "updated_at": now,
        }
        self.private_values = (
            PRIVATE_STATEMENT,
            PRIVATE_MESSAGE,
            PRIVATE_JUDGMENT,
        )
        self.delete_calls = []

    def list_case_metadata(self, limit, offset):
        self.last_page = (limit, offset)
        rows = [dict(self.metadata)] if self.metadata else []
        return {"total": len(rows), "cases": rows}

    def get_case_admin_metadata(self, case_id):
        if self.metadata and case_id == self.metadata["case_id"]:
            return dict(self.metadata)
        return None

    def delete_case_exact(self, case_id):
        self.delete_calls.append(case_id)
        if not self.metadata or case_id != self.metadata["case_id"]:
            return None
        self.metadata = None
        return {
            "case_id": case_id,
            "deleted_counts": {
                "cases": 1,
                "statements": 2,
                "artifacts": 3,
                "messages": 4,
                "case_notifications": 1,
            },
            "residual_counts": {
                "cases": 0,
                "statements": 0,
                "artifacts": 0,
                "messages": 0,
                "case_notifications": 0,
            },
            "residual": 0,
        }


class AdminRouteUnitTests(unittest.TestCase):
    def test_route_requires_exact_configured_key(self):
        self.assertFalse(is_admin_route({}, ROUTE_KEY))
        self.assertFalse(is_admin_route({"console": "wrong"}, ROUTE_KEY))
        self.assertFalse(is_admin_route({"console": [ROUTE_KEY]}, ROUTE_KEY))
        self.assertFalse(is_admin_route({"console": ROUTE_KEY}, ""))
        self.assertTrue(is_admin_route({"console": ROUTE_KEY}, ROUTE_KEY))


class AdminConsoleAppTests(unittest.TestCase):
    def setUp(self):
        self.app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")

    def _new_app(self, route_value=None):
        app = AppTest.from_file(self.app_path)
        if route_value is not None:
            app.query_params["console"] = route_value
        return app

    def _run(self, app, database=None, include_route=True, include_secret=True):
        environment = app_environment(
            database,
            include_route=include_route,
            include_secret=include_secret,
        )
        database_patch = (
            patch("db.Database", return_value=database)
            if database is not None
            else patch("db.Database", side_effect=AssertionError("Database called"))
        )
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "streamlit.runtime.secrets.Secrets.load_if_toml_exists",
                return_value=False,
            ),
            database_patch as database_class,
            patch("llm.call_llm", side_effect=AssertionError("Real LLM called")) as llm,
            patch("streamlit.dialog", new=inline_dialog),
        ):
            app.run(timeout=15)
        return database_class, llm

    def test_normal_and_wrong_routes_expose_no_admin_console(self):
        for route_value in (None, "wrong-route"):
            with self.subTest(route_value=route_value):
                app = self._new_app(route_value)
                self._run(app)
                text = visible_text(app)
                self.assertIn("双向关系仲裁员", text)
                self.assertNotIn("Case maintenance console", text)
                self.assertNotIn("Maintenance sign in", text)

    def test_correct_route_without_auth_shows_only_login_and_does_not_query_db(self):
        app = self._new_app(ROUTE_KEY)

        database_class, llm = self._run(app)

        text = visible_text(app)
        self.assertIn("Case maintenance console", text)
        self.assertIn("Maintenance sign in", text)
        self.assertNotIn("双向关系仲裁员", text)
        self.assertNotIn("Total cases", text)
        self.assertEqual(database_class.mock_calls, [])
        self.assertEqual(llm.mock_calls, [])

    def test_wrong_secret_exposes_no_metadata_and_performs_no_db_action(self):
        app = self._new_app(ROUTE_KEY)
        self._run(app)
        find(app.text_input, "Maintenance secret").set_value("wrong-secret")
        find(app.button, "Sign in").click()

        database_class, llm = self._run(app)

        text = visible_text(app)
        self.assertIn("Sign-in failed.", text)
        self.assertNotIn("Total cases", text)
        self.assertNotIn("wrong-secret", repr(app.session_state))
        self.assertEqual(database_class.mock_calls, [])
        self.assertEqual(llm.mock_calls, [])

    def test_missing_maintenance_secret_is_unavailable_without_normal_ui(self):
        app = self._new_app(ROUTE_KEY)

        self._run(app, include_secret=False)

        text = visible_text(app)
        self.assertIn("Maintenance console unavailable.", text)
        self.assertNotIn("双向关系仲裁员", text)
        self.assertNotIn("Maintenance sign in", text)

    def test_authenticated_dashboard_search_and_delete_are_private_and_rerun_safe(self):
        database = FakeAdminDatabase()
        app = self._new_app(ROUTE_KEY)
        self._run(app, database)
        find(app.text_input, "Maintenance secret").set_value(MAINTENANCE_SECRET)
        find(app.button, "Sign in").click()
        self._run(app, database)

        text = visible_text(app)
        self.assertIn("Total cases", text)
        self.assertIn(CASE_ID, text)
        self.assertNotIn(MAINTENANCE_SECRET, repr(app.session_state))
        self.assertNotIn("双向关系仲裁员", text)
        self.assertNotIn(MAINTENANCE_SECRET, text)
        self.assertNotIn(ROUTE_KEY, text)
        for canary in database.private_values:
            self.assertNotIn(canary, text)
        self.assertEqual(database.last_page, (25, 0))

        find(app.text_input, "Full Case ID").set_value(CASE_ID)
        find(app.button, "Find case").click()
        self._run(app, database)
        self.assertEqual(database.delete_calls, [])

        find(app.button, "Permanently delete").click()
        self._run(app, database)
        find(app.button, "Cancel").click()
        self._run(app, database)
        self.assertEqual(database.delete_calls, [])

        find(app.button, "Permanently delete").click()
        self._run(app, database)
        confirmation = find(app.text_input, "Type the full Case ID to confirm")
        confirmation.set_value("CASE-ABC12")
        self._run(app, database)
        self.assertTrue(find(app.button, "Delete permanently").disabled)
        self.assertEqual(database.delete_calls, [])

        confirmation = find(app.text_input, "Type the full Case ID to confirm")
        confirmation.set_value(CASE_ID)
        self._run(app, database)
        self.assertFalse(find(app.button, "Delete permanently").disabled)

        self._run(app, database)
        self.assertEqual(database.delete_calls, [])

        find(app.button, "Delete permanently").click()
        _database_class, llm = self._run(app, database)
        self.assertEqual(database.delete_calls, [CASE_ID])
        self.assertNotIn("_admin_delete_case_id", app.session_state)
        self.assertEqual(llm.mock_calls, [])
        text = visible_text(app)
        self.assertIn(f"{CASE_ID} deleted.", text)
        self.assertIn("Residual", text)


if __name__ == "__main__":
    unittest.main()
