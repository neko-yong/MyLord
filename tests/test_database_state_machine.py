import unittest
from contextlib import contextmanager

from db import CaseStateError, Database


class Result:
    def __init__(self, row=None, rows=None, rowcount=1):
        self.row = row
        self.rows = rows if rows is not None else []
        self.rowcount = rowcount

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class ScriptedConnection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, statement, params=()):
        self.calls.append((" ".join(statement.split()), params))
        if not self.responses:
            raise AssertionError("unexpected SQL execution")
        return self.responses.pop(0)


def database_with(*responses):
    database = Database.__new__(Database)
    connection = ScriptedConnection(responses)

    @contextmanager
    def connection_scope():
        yield connection

    database._connection = connection_scope
    return database, connection


class ArbitrationStateMachineTests(unittest.TestCase):
    def test_statement_database_gate_rejects_empty_content(self):
        for content in ("", " ", "\n", "     "):
            with self.subTest(content=repr(content)):
                database, connection = database_with()

                with self.assertRaises(ValueError):
                    database.save_statement("CASE-TEST", "A", content)

                self.assertEqual(connection.calls, [])

    def test_mediating_and_map_ready_can_request(self):
        for status in ("MEDIATING", "MAP_READY"):
            with self.subTest(status=status):
                requested = {
                    "status": "ARBITRATION_PENDING",
                    "arbitration_requested_by": "A",
                    "arbitration_requested_at": "now",
                }
                database, _connection = database_with(
                    Result(row={"status": status, "arbitration_requested_by": None}),
                    Result(row=requested),
                )

                result = database.request_arbitration("CASE-TEST", "A")

                self.assertEqual(result["status"], "ARBITRATION_PENDING")
                self.assertEqual(result["arbitration_requested_by"], "A")

    def test_concurrent_second_request_keeps_first_requester(self):
        pending = {
            "status": "ARBITRATION_PENDING",
            "arbitration_requested_by": "A",
            "arbitration_requested_at": "first",
        }
        database, connection = database_with(Result(row=pending))

        result = database.request_arbitration("CASE-TEST", "B")

        self.assertEqual(result["arbitration_requested_by"], "A")
        self.assertEqual(len(connection.calls), 1)

    def test_requester_cannot_confirm_own_request(self):
        database, _connection = database_with(
            Result(
                row={
                    "status": "ARBITRATION_PENDING",
                    "arbitration_requested_by": "A",
                }
            )
        )

        with self.assertRaises(CaseStateError):
            database.confirm_arbitration("CASE-TEST", "A")

    def test_cancel_returns_map_ready_without_messages(self):
        database, _connection = database_with(
            Result(
                row={
                    "status": "ARBITRATION_PENDING",
                    "arbitration_requested_by": "A",
                }
            ),
            Result(row={"count": 0}),
            Result(row={"status": "MAP_READY"}),
        )

        result = database.cancel_arbitration_request("CASE-TEST", "A")

        self.assertEqual(result["status"], "MAP_READY")

    def test_cancel_returns_mediating_when_pending_messages_exist(self):
        database, _connection = database_with(
            Result(
                row={
                    "status": "ARBITRATION_PENDING",
                    "arbitration_requested_by": "A",
                }
            ),
            Result(row={"count": 1}),
            Result(row={"status": "MEDIATING"}),
        )

        result = database.cancel_arbitration_request("CASE-TEST", "B")

        self.assertEqual(result["status"], "MEDIATING")

    def test_pending_message_is_allowed_without_cancelling_request(self):
        database, connection = database_with(
            Result(row={"status": "ARBITRATION_PENDING"}),
            Result(),
            Result(),
        )

        database.add_message("CASE-TEST", "A", "pending message")

        update_sql = connection.calls[-1][0]
        self.assertIn("ARBITRATION_PENDING", update_sql)

    def test_arbitrating_and_closed_messages_are_rejected(self):
        for status in ("ARBITRATING", "CLOSED"):
            with self.subTest(status=status):
                database, _connection = database_with(Result(row={"status": status}))
                with self.assertRaises(CaseStateError):
                    database.add_message("CASE-TEST", "B", "forbidden")

    def test_judge_intervention_gate(self):
        database, _connection = database_with(
            Result(row={"status": "ARBITRATION_PENDING"})
        )
        self.assertTrue(database.ensure_judge_intervention_allowed("CASE-TEST"))

        for status in ("ARBITRATING", "CLOSED"):
            with self.subTest(status=status):
                database, _connection = database_with(Result(row={"status": status}))
                with self.assertRaises(CaseStateError):
                    database.ensure_judge_intervention_allowed("CASE-TEST")

    def test_pause_and_resume_do_not_accept_arbitrating(self):
        database, pause_connection = database_with(Result(rowcount=0))
        self.assertFalse(database.pause_case("CASE-TEST", "A"))
        self.assertNotIn("ARBITRATING", pause_connection.calls[0][0])

        database, resume_connection = database_with(Result(rowcount=0))
        self.assertFalse(database.resume_case("CASE-TEST", "A"))
        self.assertNotIn("ARBITRATING", resume_connection.calls[0][0])


if __name__ == "__main__":
    unittest.main()
