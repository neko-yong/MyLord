import time
import unittest
from unittest.mock import Mock, patch

import database_resources
from db import Database


class DatabaseResourceLifecycleTests(unittest.TestCase):
    def setUp(self):
        database_resources.get_postgres_pool.clear()

    def tearDown(self):
        database_resources.get_postgres_pool.clear()

    def test_twenty_fresh_wrappers_reuse_one_cached_pool(self):
        shared_pool = Mock()
        constructor_calls = []
        pool_calls = []

        class DatabaseSpy:
            @staticmethod
            def create_pool(database_url):
                pool_calls.append(database_url)
                return shared_pool

            def __init__(self, pool=None):
                constructor_calls.append(pool)
                self.pool = pool

        with (
            patch.object(
                database_resources.db_module,
                "Database",
                DatabaseSpy,
            ),
            patch.object(
                database_resources.db_module,
                "initialize_postgres_schema",
            ) as initialize_schema,
            patch.object(database_resources.atexit, "register") as register,
        ):
            wrappers = [
                database_resources.get_database("postgresql://cache-key")
                for _ in range(20)
            ]

        self.assertEqual(len({id(wrapper) for wrapper in wrappers}), 20)
        self.assertTrue(all(wrapper.pool is shared_pool for wrapper in wrappers))
        self.assertEqual(constructor_calls, [shared_pool] * 20)
        self.assertEqual(pool_calls, ["postgresql://cache-key"])
        initialize_schema.assert_called_once_with(shared_pool)
        register.assert_called_once_with(shared_pool.close)

    def test_cached_pool_does_not_preserve_old_business_class(self):
        shared_pool = Mock()
        pool_calls = []

        class CachedDatabaseV1:
            @staticmethod
            def create_pool(database_url):
                pool_calls.append(database_url)
                return shared_pool

            def __init__(self, pool=None):
                self.pool = pool

        class CurrentDatabaseV2:
            @staticmethod
            def create_pool(_database_url):
                raise AssertionError("cached pool was recreated")

            def __init__(self, pool=None):
                self.pool = pool

            def new_method(self):
                return "current"

        with (
            patch.object(
                database_resources.db_module,
                "initialize_postgres_schema",
            ),
            patch.object(database_resources.atexit, "register"),
            patch.object(
                database_resources.db_module,
                "Database",
                CachedDatabaseV1,
            ),
        ):
            old_wrapper = database_resources.get_database(
                "postgresql://stale-regression"
            )

        with patch.object(
            database_resources.db_module,
            "Database",
            CurrentDatabaseV2,
        ):
            current_wrapper = database_resources.get_database(
                "postgresql://stale-regression"
            )

        self.assertIsInstance(old_wrapper, CachedDatabaseV1)
        self.assertIsInstance(current_wrapper, CurrentDatabaseV2)
        self.assertEqual(current_wrapper.new_method(), "current")
        self.assertIs(current_wrapper.pool, old_wrapper.pool)
        self.assertEqual(pool_calls, ["postgresql://stale-regression"])

    def test_cache_clear_allows_a_new_pool(self):
        pools = [Mock(), Mock()]
        with (
            patch.object(
                Database,
                "create_pool",
                side_effect=pools,
            ) as create_pool,
            patch.object(
                database_resources.db_module,
                "initialize_postgres_schema",
            ),
            patch.object(database_resources.atexit, "register"),
        ):
            first = database_resources.get_database(
                "postgresql://cache-clear"
            )
            database_resources.get_postgres_pool.clear()
            second = database_resources.get_database(
                "postgresql://cache-clear"
            )

        self.assertIs(first.pool, pools[0])
        self.assertIs(second.pool, pools[1])
        self.assertEqual(create_pool.call_count, 2)

    def test_factory_returns_current_database_api(self):
        shared_pool = Mock()
        with (
            patch.object(
                Database,
                "create_pool",
                return_value=shared_pool,
            ),
            patch.object(
                database_resources.db_module,
                "initialize_postgres_schema",
            ),
            patch.object(database_resources.atexit, "register"),
        ):
            wrapper = database_resources.get_database(
                "postgresql://current-api"
            )

        self.assertIsInstance(wrapper, Database)
        self.assertTrue(hasattr(wrapper, "get_arbitration_evidence"))
        self.assertTrue(hasattr(wrapper, "get_unread_notifications"))
        self.assertTrue(hasattr(wrapper, "mark_notification_read"))

    def test_externally_owned_pool_is_not_closed_by_wrapper(self):
        shared_pool = Mock()
        wrapper = Database(pool=shared_pool)

        wrapper.close()

        shared_pool.close.assert_not_called()

    def test_owned_pool_is_closed_by_wrapper(self):
        owned_pool = Mock()
        with patch.object(Database, "create_pool", return_value=owned_pool):
            wrapper = Database("postgresql://owned-pool")

        wrapper.close()

        owned_pool.close.assert_called_once_with()

    def test_wrapper_is_lightweight_and_stateless(self):
        shared_pool = Mock()
        started = time.perf_counter()
        wrappers = [Database(pool=shared_pool) for _ in range(10_000)]
        elapsed_ms = (time.perf_counter() - started) * 1000 / len(wrappers)

        self.assertLess(elapsed_ms, 10.0)
        self.assertEqual(
            set(wrappers[-1].__dict__),
            {"_pool_timeout", "_owns_pool", "_pool"},
        )


if __name__ == "__main__":
    unittest.main()
