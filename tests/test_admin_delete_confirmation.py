import unittest
from unittest.mock import patch

import streamlit as st

from db import DatabaseError
from tests import test_admin_console as admin_tests


ACKNOWLEDGEMENT = "我确认删除这个案件，且无法恢复"
OTHER_CASE_ID = "CASE-KEEP456"


class TwoCaseDatabase:
    def __init__(self):
        self.cases = {}
        for case_id in (admin_tests.CASE_ID, OTHER_CASE_ID):
            database = admin_tests.FakeAdminDatabase()
            database.metadata["case_id"] = case_id
            self.cases[case_id] = database
        self.delete_calls = []

    def list_case_metadata(self, limit, offset):
        rows = [dict(db.metadata) for db in self.cases.values() if db.metadata]
        return {"total": len(rows), "cases": rows[offset : offset + limit]}

    def get_case_admin_metadata(self, case_id):
        database = self.cases.get(case_id)
        return database.get_case_admin_metadata(case_id) if database else None

    def delete_case_exact(self, case_id):
        self.delete_calls.append(case_id)
        return self.cases[case_id].delete_case_exact(case_id)


class AdminDeleteConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.harness = admin_tests.AdminConsoleAppTests()
        self.harness.setUp()
        self.database = TwoCaseDatabase()
        self.app = self.harness._new_app(admin_tests.ROUTE_KEY)
        self._run()
        self._login()

    def _run(self):
        _, llm = self.harness._run(self.app, self.database)
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(llm.mock_calls, [])

    def _login(self):
        admin_tests.find(self.app.text_input, "Maintenance secret").set_value(
            admin_tests.MAINTENANCE_SECRET
        )
        self._click("Sign in")

    def _click(self, label):
        admin_tests.find(self.app.button, label).click()
        self._run()

    def _search(self, case_id=admin_tests.CASE_ID):
        admin_tests.find(self.app.text_input, "Full Case ID").set_value(case_id)
        self._click("Find case")

    def _open(self, case_id=admin_tests.CASE_ID):
        self._search(case_id)
        self._click("Permanently delete")

    def _acknowledge(self):
        admin_tests.find(self.app.checkbox, ACKNOWLEDGEMENT).check()
        self._run()

    def _assert_confirmation_cleared(self):
        state = self.app.session_state.filtered_state
        self.assertNotIn("_admin_delete_case_id", state)
        self.assertFalse(any(
            key.startswith("admin_delete_acknowledged_") for key in state
        ))

    def test_id_is_entered_once_and_unchecked_submit_cannot_delete(self):
        self._open()
        self.assertEqual([item.label for item in self.app.text_input], ["Full Case ID"])
        self.assertFalse(admin_tests.find(self.app.checkbox, ACKNOWLEDGEMENT).value)
        self.assertTrue(admin_tests.find(self.app.button, "Delete permanently").disabled)
        # Bypass only the disabled UI property to exercise the server-side guard.
        real_button = st.button

        def enabled_button(label, *args, **kwargs):
            if label == "Delete permanently":
                kwargs["disabled"] = False
            return real_button(label, *args, **kwargs)

        with patch("streamlit.button", side_effect=enabled_button):
            self._click("Delete permanently")
        self.assertIn("Delete not confirmed", admin_tests.visible_text(self.app))
        self.assertEqual(self.database.delete_calls, [])
        self.assertIsNotNone(self.database.get_case_admin_metadata(admin_tests.CASE_ID))

    def test_cancel_and_reopen_requires_a_new_acknowledgement(self):
        self._open()
        self._acknowledge()
        self._click("Cancel")
        self._assert_confirmation_cleared()
        self._click("Permanently delete")
        self.assertFalse(admin_tests.find(self.app.checkbox, ACKNOWLEDGEMENT).value)
        self.assertEqual(self.database.delete_calls, [])

    def test_confirmation_keeps_only_existing_safe_metadata(self):
        private_values = self.database.cases[admin_tests.CASE_ID].private_values
        self.database.cases[admin_tests.CASE_ID].metadata.update({
            "content": private_values[0],
            "token": private_values[1],
            "a_token_hash": private_values[2],
        })
        self._open()
        text = admin_tests.visible_text(self.app)
        for value in (*private_values, admin_tests.ROUTE_KEY, admin_tests.MAINTENANCE_SECRET):
            self.assertNotIn(value, text)
        self.assertIn("当前安全元数据未提供标题和关联记录数量", text)
        self.assertIn(admin_tests.CASE_ID, text)
        self.assertEqual(self.database.delete_calls, [])

    def test_unchecking_revokes_confirmation_without_a_write(self):
        self._open()
        self._acknowledge()
        admin_tests.find(self.app.checkbox, ACKNOWLEDGEMENT).uncheck()
        self._run()
        self.assertTrue(admin_tests.find(self.app.button, "Delete permanently").disabled)
        self._click("Delete permanently")
        self.assertEqual(self.database.delete_calls, [])

    def test_every_new_search_discards_old_confirmation(self):
        for requested in (admin_tests.CASE_ID, OTHER_CASE_ID, "CASE-MISSING"):
            with self.subTest(requested=requested):
                self._open()
                self._acknowledge()
                self._search(requested)
                self._assert_confirmation_cleared()
                self.assertEqual(self.database.delete_calls, [])
                if requested == "CASE-MISSING":
                    self.assertNotIn("_admin_selected_case_id", self.app.session_state)
                    self.assertFalse(self.app.checkbox)
                else:
                    self._click("Permanently delete")
                    self.assertFalse(admin_tests.find(self.app.checkbox, ACKNOWLEDGEMENT).value)

    def test_confirmation_for_a_does_not_authorize_b(self):
        self._open()
        self._acknowledge()
        stale_confirm = admin_tests.find(self.app.button, "Delete permanently")
        self._search(OTHER_CASE_ID)
        stale_confirm.click()
        self._run()
        self._click("Permanently delete")
        self._click("Delete permanently")
        self.assertEqual(self.database.delete_calls, [])
        self.assertIsNotNone(self.database.get_case_admin_metadata(OTHER_CASE_ID))

    def test_unsubmitted_search_text_is_never_the_delete_target(self):
        self._open()
        self._acknowledge()
        admin_tests.find(self.app.text_input, "Full Case ID").set_value(OTHER_CASE_ID)
        self._click("Delete permanently")
        self.assertEqual(self.database.delete_calls, [admin_tests.CASE_ID])
        self.assertIsNotNone(self.database.get_case_admin_metadata(OTHER_CASE_ID))

    def test_selected_target_mismatch_rejects_stale_confirmation(self):
        self._open()
        self._acknowledge()
        self.app.session_state["_admin_selected_case_id"] = OTHER_CASE_ID
        self._click("Delete permanently")
        self.assertEqual(self.database.delete_calls, [])
        self._assert_confirmation_cleared()

    def test_final_execution_rechecks_target_after_render(self):
        self._open()
        self._acknowledge()
        real_button = st.button

        def changed_target(label, *args, **kwargs):
            clicked = real_button(label, *args, **kwargs)
            if label == "Delete permanently" and clicked:
                st.session_state["_admin_selected_case_id"] = OTHER_CASE_ID
            return clicked

        with patch("streamlit.button", side_effect=changed_target):
            self._click("Delete permanently")
        self.assertEqual(self.database.delete_calls, [])

    def test_lost_authentication_blocks_a_pending_delete(self):
        self._open()
        self._acknowledge()
        self.app.session_state["_admin_authenticated"] = False
        self._click("Delete permanently")
        self._assert_confirmation_cleared()
        self.assertIn("Maintenance sign in", admin_tests.visible_text(self.app))
        self.assertEqual(self.database.delete_calls, [])

    def test_repeated_submit_and_reruns_delete_only_once(self):
        self._open()
        self._acknowledge()
        final_button = admin_tests.find(self.app.button, "Delete permanently")
        self._click("Delete permanently")
        self._assert_confirmation_cleared()
        final_button.click()
        self._run()
        self._run()
        self.assertEqual(self.database.delete_calls, [admin_tests.CASE_ID])
        self.assertIsNotNone(self.database.get_case_admin_metadata(OTHER_CASE_ID))

    def test_logout_and_new_browser_session_have_no_old_authorization(self):
        self._open()
        self._acknowledge()
        self._click("Sign out")
        self._assert_confirmation_cleared()
        self._login()
        self.assertNotIn("_admin_selected_case_id", self.app.session_state)
        self._open()
        self.assertFalse(admin_tests.find(self.app.checkbox, ACKNOWLEDGEMENT).value)
        self._acknowledge()
        # A browser refresh creates a fresh Streamlit session, not just a rerun.
        self.app = self.harness._new_app(admin_tests.ROUTE_KEY)
        self._run()
        self.assertIn("Maintenance sign in", admin_tests.visible_text(self.app))
        self._assert_confirmation_cleared()
        self._login()
        self.assertNotIn("_admin_selected_case_id", self.app.session_state)
        self.assertEqual(self.database.delete_calls, [])

    def test_missing_case_and_delete_race_are_safe(self):
        self._open()
        self._acknowledge()
        self.database.cases[admin_tests.CASE_ID].metadata = None
        self._click("Delete permanently")
        self.assertEqual(self.database.delete_calls, [])
        self._assert_confirmation_cleared()
        self.assertIn("already deleted", admin_tests.visible_text(self.app))

        self._open(OTHER_CASE_ID)
        self._acknowledge()
        with patch.object(self.database, "delete_case_exact", return_value=None) as delete:
            self._click("Delete permanently")
        delete.assert_called_once_with(OTHER_CASE_ID)
        self._assert_confirmation_cleared()
        self.assertIn("already deleted", admin_tests.visible_text(self.app))

    def test_search_and_delete_errors_clear_old_authorization(self):
        self._open()
        self._acknowledge()
        with patch.object(
            self.database, "get_case_admin_metadata",
            side_effect=DatabaseError("Safe search failure"),
        ):
            self._search(OTHER_CASE_ID)
        self._assert_confirmation_cleared()
        self.assertNotIn("_admin_selected_case_id", self.app.session_state)

        self._open()
        self._acknowledge()
        with patch.object(
            self.database, "delete_case_exact",
            side_effect=DatabaseError("Safe deletion failure"),
        ) as delete:
            self._click("Delete permanently")
        delete.assert_called_once_with(admin_tests.CASE_ID)
        self._assert_confirmation_cleared()
        self._run()
        self.assertEqual(self.database.delete_calls, [])


if __name__ == "__main__":
    unittest.main()
