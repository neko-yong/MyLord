import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from db import ADMIN_CASE_LINKED_TABLES, Database, DatabaseError
from integration_config import load_test_database_url


TEST_DATABASE_URL, _TEST_DATABASE_SOURCE = load_test_database_url()
PRIVATE_STATEMENT = "PRIVATE_STATEMENT_CANARY_92X"
PRIVATE_MESSAGE = "PRIVATE_MESSAGE_CANARY_31K"
PRIVATE_JUDGMENT = "PRIVATE_JUDGMENT_CANARY_77P"


def graph_counts(database, case_id):
    with database._connection() as connection:
        return database._admin_case_counts(connection, case_id)


def insert_case_graph(database, case_id, marker):
    with database._connection() as connection:
        connection.execute(
            """
            INSERT INTO cases(
                case_id, title, a_token_hash, b_token_hash,
                status, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, 'MEDIATING', NOW(), NOW())
            """,
            (case_id, f"Admin integration {marker}", "a" * 64, "b" * 64),
        )
        connection.execute(
            """
            INSERT INTO statements(case_id, role, content)
            VALUES (%s, 'A', %s), (%s, 'B', %s)
            """,
            (
                case_id,
                f"{PRIVATE_STATEMENT}_{marker}_A",
                case_id,
                f"{PRIVATE_STATEMENT}_{marker}_B",
            ),
        )
        connection.execute(
            """
            INSERT INTO artifacts(case_id, kind, content)
            VALUES
                (%s, 'DISPUTE_MAP', %s),
                (%s, 'FINAL_JUDGMENT', %s)
            """,
            (
                case_id,
                f"PRIVATE_MAP_{marker}",
                case_id,
                f"{PRIVATE_JUDGMENT}_{marker}",
            ),
        )
        connection.execute(
            """
            INSERT INTO messages(case_id, sender, content)
            VALUES (%s, 'A', %s), (%s, 'JUDGE', %s)
            """,
            (
                case_id,
                f"{PRIVATE_MESSAGE}_{marker}_A",
                case_id,
                f"{PRIVATE_MESSAGE}_{marker}_JUDGE",
            ),
        )
        connection.execute(
            """
            INSERT INTO case_notifications(
                case_id, recipient_role, event_type, actor_role
            )
            VALUES (%s, 'A', 'ARBITRATION_DECLINED', 'B')
            """,
            (case_id,),
        )


def cleanup_cases(database, case_ids):
    with database._connection() as connection:
        connection.execute(
            "DELETE FROM cases WHERE case_id = ANY(%s)",
            (list(case_ids),),
        )


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "ADMIN_POSTGRES_DELETE = NOT RUN (TEST_DATABASE_URL is not set)",
)
class AdminPostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = Database(TEST_DATABASE_URL, min_size=1, max_size=2)
        cls.database.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.database.close()

    def _ids(self):
        run_id = uuid.uuid4().hex.upper()
        return (
            f"TEST-ADMIN-DELETE-{run_id}",
            f"TEST-ADMIN-KEEP-{run_id}",
        )

    def test_exact_delete_cascades_and_preserves_unrelated_graph(self):
        target_case_id, keep_case_id = self._ids()
        try:
            insert_case_graph(self.database, target_case_id, "DELETE")
            insert_case_graph(self.database, keep_case_id, "KEEP")
            target_before = graph_counts(self.database, target_case_id)
            keep_before = graph_counts(self.database, keep_case_id)

            metadata = self.database.list_case_metadata(limit=100, offset=0)
            exact = self.database.get_case_admin_metadata(target_case_id)
            private_capture = f"{metadata!r}\n{exact!r}"
            for canary in (
                PRIVATE_STATEMENT,
                PRIVATE_MESSAGE,
                PRIVATE_JUDGMENT,
            ):
                self.assertNotIn(canary, private_capture)

            for non_exact in (
                target_case_id[:-1],
                target_case_id.lower(),
                target_case_id + "%",
                "%",
                "_",
                "' OR 1=1 --",
            ):
                with self.subTest(non_exact=non_exact):
                    self.assertIsNone(self.database.delete_case_exact(non_exact))
                    self.assertEqual(
                        graph_counts(self.database, target_case_id),
                        target_before,
                    )
                    self.assertEqual(
                        graph_counts(self.database, keep_case_id),
                        keep_before,
                    )

            result = self.database.delete_case_exact(target_case_id)

            self.assertEqual(result["deleted_counts"], target_before)
            self.assertEqual(result["residual"], 0)
            self.assertEqual(
                graph_counts(self.database, target_case_id),
                {table: 0 for table in ADMIN_CASE_LINKED_TABLES},
            )
            self.assertEqual(
                graph_counts(self.database, keep_case_id),
                keep_before,
            )
            self.assertIsNone(self.database.delete_case_exact(target_case_id))
        finally:
            cleanup_cases(self.database, (target_case_id, keep_case_id))

    def test_controlled_failure_rolls_back_deleted_graph(self):
        target_case_id, keep_case_id = self._ids()
        try:
            insert_case_graph(self.database, target_case_id, "ROLLBACK")
            insert_case_graph(self.database, keep_case_id, "ROLLBACK_KEEP")
            target_before = graph_counts(self.database, target_case_id)
            keep_before = graph_counts(self.database, keep_case_id)
            real_counts = self.database._admin_case_counts
            calls = 0

            def fail_after_delete(connection, case_id):
                nonlocal calls
                calls += 1
                counts = real_counts(connection, case_id)
                if calls == 2:
                    raise DatabaseError("controlled rollback fault")
                return counts

            with patch.object(
                self.database,
                "_admin_case_counts",
                side_effect=fail_after_delete,
            ):
                with self.assertRaises(DatabaseError):
                    self.database.delete_case_exact(target_case_id)

            self.assertEqual(
                graph_counts(self.database, target_case_id),
                target_before,
            )
            self.assertEqual(
                graph_counts(self.database, keep_case_id),
                keep_before,
            )
        finally:
            cleanup_cases(self.database, (target_case_id, keep_case_id))

    def test_concurrent_delete_and_metadata_read_are_atomic(self):
        target_case_id, keep_case_id = self._ids()
        zero_counts = {table: 0 for table in ADMIN_CASE_LINKED_TABLES}
        try:
            insert_case_graph(self.database, target_case_id, "CONCURRENT")
            insert_case_graph(self.database, keep_case_id, "CONCURRENT_KEEP")
            target_before = graph_counts(self.database, target_case_id)
            keep_before = graph_counts(self.database, keep_case_id)
            barrier = Barrier(2)

            def read_target():
                barrier.wait()
                metadata = self.database.get_case_admin_metadata(target_case_id)
                counts = graph_counts(self.database, target_case_id)
                return metadata, counts

            def delete_target():
                barrier.wait()
                return self.database.delete_case_exact(target_case_id)

            with ThreadPoolExecutor(max_workers=2) as executor:
                read_future = executor.submit(read_target)
                delete_future = executor.submit(delete_target)
                metadata, observed_counts = read_future.result(timeout=30)
                delete_result = delete_future.result(timeout=30)

            self.assertEqual(delete_result["deleted_counts"], target_before)
            self.assertEqual(delete_result["residual"], 0)
            self.assertIn(observed_counts, (target_before, zero_counts))
            if metadata is not None:
                self.assertEqual(metadata["case_id"], target_case_id)
                private_capture = repr(metadata)
                for canary in (
                    PRIVATE_STATEMENT,
                    PRIVATE_MESSAGE,
                    PRIVATE_JUDGMENT,
                ):
                    self.assertNotIn(canary, private_capture)

            self.assertEqual(
                graph_counts(self.database, target_case_id),
                zero_counts,
            )
            self.assertEqual(
                graph_counts(self.database, keep_case_id),
                keep_before,
            )
        finally:
            cleanup_cases(self.database, (target_case_id, keep_case_id))


if __name__ == "__main__":
    unittest.main()
