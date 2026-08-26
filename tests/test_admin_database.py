import copy
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from db import ADMIN_CASE_LINKED_TABLES, Database, DatabaseError


TARGET_CASE_ID = "CASE-ABC123"
KEEP_CASE_ID = "CASE-KEEP99"


class FakeResult:
    def __init__(self, row=None, rowcount=0):
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection
        self.snapshot = None

    def __enter__(self):
        self.snapshot = copy.deepcopy(self.connection.store)
        return self

    def __exit__(self, exc_type, _exc, _traceback):
        if exc_type is not None:
            self.connection.store.clear()
            self.connection.store.update(self.snapshot)
        return False


class FakeConnection:
    def __init__(self, store, cascade=True):
        self.store = store
        self.cascade = cascade
        self.executions = []

    def transaction(self):
        return FakeTransaction(self)

    def execute(self, statement, params=()):
        normalized = " ".join(statement.split()).upper()
        self.executions.append((normalized, params))
        case_id = params[0] if params else None

        if normalized.startswith("SELECT CASE_ID FROM CASES"):
            row = (
                {"case_id": case_id}
                if case_id in self.store["cases"]
                else None
            )
            return FakeResult(row=row)
        if normalized.startswith("SELECT (SELECT COUNT(*) FROM CASES"):
            return FakeResult(
                row={
                    table: self.store[table].get(case_id, 0)
                    for table in ADMIN_CASE_LINKED_TABLES
                }
            )
        if normalized.startswith("DELETE FROM CASES"):
            existed = case_id in self.store["cases"]
            self.store["cases"].pop(case_id, None)
            if self.cascade:
                for table in ADMIN_CASE_LINKED_TABLES[1:]:
                    self.store[table].pop(case_id, None)
            return FakeResult(rowcount=1 if existed else 0)
        raise AssertionError(f"Unexpected SQL: {normalized}")


class FakePool:
    def __init__(self, connection):
        self._connection = connection

    @contextmanager
    def connection(self, timeout):
        del timeout
        yield self._connection


def case_store():
    return {
        "cases": {TARGET_CASE_ID: 1, KEEP_CASE_ID: 1},
        "statements": {TARGET_CASE_ID: 2, KEEP_CASE_ID: 1},
        "artifacts": {TARGET_CASE_ID: 3, KEEP_CASE_ID: 1},
        "messages": {TARGET_CASE_ID: 4, KEEP_CASE_ID: 2},
        "case_notifications": {TARGET_CASE_ID: 1, KEEP_CASE_ID: 1},
    }


def fake_database(cascade=True):
    connection = FakeConnection(case_store(), cascade=cascade)
    database = Database(pool=FakePool(connection))
    return database, connection


class AdminMetadataTests(unittest.TestCase):
    def test_list_metadata_is_paginated_and_private_fields_are_not_queried(self):
        now = datetime.now(timezone.utc)
        database = Database.__new__(Database)
        database._read_query = Mock(
            side_effect=[
                {"count": 1},
                [
                    {
                        "case_id": TARGET_CASE_ID,
                        "status": "MEDIATING",
                        "created_at": now,
                        "updated_at": now,
                    }
                ],
            ]
        )

        result = database.list_case_metadata(limit=25, offset=50)

        self.assertEqual(result["total"], 1)
        self.assertEqual(
            set(result["cases"][0]),
            {"case_id", "status", "created_at", "updated_at"},
        )
        query = " ".join(database._read_query.call_args_list[1].args[0].split())
        self.assertIn("ORDER BY created_at DESC, case_id DESC", query)
        self.assertEqual(database._read_query.call_args_list[1].args[1], (25, 50))
        for private_column in (
            "title",
            "content",
            "token",
            "evidence_hash",
        ):
            self.assertNotIn(private_column, query.lower())

    def test_exact_lookup_uses_parameter_equality_and_minimal_columns(self):
        now = datetime.now(timezone.utc)
        database = Database.__new__(Database)
        database._read_query = Mock(
            return_value={
                "case_id": TARGET_CASE_ID,
                "status": "COLLECTING",
                "created_at": now,
                "updated_at": now,
            }
        )

        result = database.get_case_admin_metadata(TARGET_CASE_ID)

        self.assertEqual(result["case_id"], TARGET_CASE_ID)
        statement, params = database._read_query.call_args.args
        normalized = " ".join(statement.split()).upper()
        self.assertIn("WHERE CASE_ID = %S", normalized)
        self.assertNotIn("LIKE", normalized)
        self.assertEqual(params, (TARGET_CASE_ID,))

    def test_pagination_bounds_are_server_validated(self):
        database = Database.__new__(Database)
        database._read_query = Mock()

        for limit, offset in ((0, 0), (101, 0), (True, 0), (25, -1)):
            with self.subTest(limit=limit, offset=offset):
                with self.assertRaises(ValueError):
                    database.list_case_metadata(limit=limit, offset=offset)
        database._read_query.assert_not_called()


class AdminDeleteTests(unittest.TestCase):
    def test_exact_delete_removes_target_and_preserves_unrelated_case(self):
        database, connection = fake_database()
        keep_before = {
            table: connection.store[table][KEEP_CASE_ID]
            for table in ADMIN_CASE_LINKED_TABLES
        }

        result = database.delete_case_exact(TARGET_CASE_ID)

        self.assertEqual(result["case_id"], TARGET_CASE_ID)
        self.assertEqual(result["residual"], 0)
        self.assertEqual(
            result["deleted_counts"],
            {
                "cases": 1,
                "statements": 2,
                "artifacts": 3,
                "messages": 4,
                "case_notifications": 1,
            },
        )
        for table in ADMIN_CASE_LINKED_TABLES:
            self.assertNotIn(TARGET_CASE_ID, connection.store[table])
            self.assertEqual(connection.store[table][KEEP_CASE_ID], keep_before[table])

        delete_statements = [
            (statement, params)
            for statement, params in connection.executions
            if statement.startswith("DELETE")
        ]
        self.assertEqual(len(delete_statements), 1)
        self.assertIn("WHERE CASE_ID = %S", delete_statements[0][0])
        self.assertNotIn("LIKE", delete_statements[0][0])
        self.assertEqual(delete_statements[0][1], (TARGET_CASE_ID,))

    def test_non_exact_inputs_change_no_rows(self):
        invalid_inputs = (
            None,
            "",
            " ",
            "CASE-ABC12",
            "CASE-ABC",
            "ABC123",
            "CASE-ABC123 ",
            "case-abc123",
            "CASE-ABC123%",
            "%",
            "_",
            "' OR 1=1 --",
            "X" * 129,
            "CASE-DIFFERENT",
        )
        for candidate in invalid_inputs:
            with self.subTest(candidate=candidate):
                database, connection = fake_database()
                before = copy.deepcopy(connection.store)
                self.assertIsNone(database.delete_case_exact(candidate))
                self.assertEqual(connection.store, before)
                self.assertFalse(
                    any(
                        statement.startswith("DELETE")
                        for statement, _params in connection.executions
                    )
                )

    def test_repeated_delete_is_idempotent(self):
        database, connection = fake_database()
        database.delete_case_exact(TARGET_CASE_ID)
        after_first = copy.deepcopy(connection.store)

        self.assertIsNone(database.delete_case_exact(TARGET_CASE_ID))
        self.assertEqual(connection.store, after_first)

    def test_fault_after_parent_delete_rolls_back_all_rows(self):
        database, connection = fake_database()
        before = copy.deepcopy(connection.store)
        real_counts = database._admin_case_counts
        calls = 0

        def fail_on_residual_check(active_connection, case_id):
            nonlocal calls
            calls += 1
            counts = real_counts(active_connection, case_id)
            if calls == 2:
                raise DatabaseError("controlled fault")
            return counts

        with patch.object(
            database,
            "_admin_case_counts",
            side_effect=fail_on_residual_check,
        ):
            with self.assertRaises(DatabaseError):
                database.delete_case_exact(TARGET_CASE_ID)

        self.assertEqual(connection.store, before)

    def test_unexplained_residual_rolls_back(self):
        database, connection = fake_database(cascade=False)
        before = copy.deepcopy(connection.store)

        with self.assertRaises(DatabaseError):
            database.delete_case_exact(TARGET_CASE_ID)

        self.assertEqual(connection.store, before)


if __name__ == "__main__":
    unittest.main()
