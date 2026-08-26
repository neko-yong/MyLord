import io
import logging
import time
import unittest

from performance import (
    finish_current_trace,
    finish_trace,
    instrument_database,
    observe_fragment,
    record_llm_call,
    start_trace,
)


PRIVATE_CANARY = "private-message-CANARY-PERF-92841"
DATABASE_CANARY = "postgresql://user:password-CANARY-PERF-92841@host/db"
TOKEN_CANARY = "A-raw-token-CANARY-PERF-92841"


class FakeDatabase:
    def get_case(self, case_id, private_value=None):
        return {"case_id": case_id, "private": private_value}


class PerformanceInstrumentationTests(unittest.TestCase):
    def capture_logs(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        perf_logger = logging.getLogger("performance")
        perf_logger.addHandler(handler)
        perf_logger.setLevel(logging.WARNING)
        self.addCleanup(perf_logger.removeHandler, handler)
        return output

    def test_disabled_database_instrumentation_preserves_object(self):
        database = FakeDatabase()
        self.assertIs(instrument_database(database, False), database)

    def test_trace_reports_safe_aggregates_without_arguments(self):
        output = self.capture_logs()
        trace = start_trace(True, "full_rerun", "app")
        database = instrument_database(FakeDatabase(), True)

        row = database.get_case(DATABASE_CANARY, PRIVATE_CANARY)
        record_llm_call("mock", 1.5, "test")
        summary = finish_trace(trace)

        self.assertEqual(row["private"], PRIVATE_CANARY)
        self.assertEqual(summary["db_calls"], 1)
        self.assertEqual(summary["llm_calls"], 1)
        visible = output.getvalue()
        self.assertIn("db_method=get_case", visible)
        self.assertIn("llm_calls=1", visible)
        for forbidden in (PRIVATE_CANARY, DATABASE_CANARY, TOKEN_CANARY):
            self.assertNotIn(forbidden, visible)

    def test_fragment_metrics_are_merged_into_parent_rerun(self):
        output = self.capture_logs()
        parent = start_trace(True, "full_rerun", "app")
        database = instrument_database(FakeDatabase(), True)

        @observe_fragment("test_fragment", True)
        def fragment():
            return database.get_case("CASE-SAFE")

        fragment()
        summary = finish_trace(parent)

        self.assertEqual(summary["db_calls"], 1)
        visible = output.getvalue()
        self.assertIn("kind=fragment name=test_fragment", visible)
        self.assertIn("kind=full_rerun name=app", visible)

    def test_finish_is_idempotent(self):
        output = self.capture_logs()
        trace = start_trace(True, "fragment", "idempotent")
        time.sleep(0.001)
        self.assertIsNotNone(finish_trace(trace))
        self.assertIsNone(finish_trace(trace))
        self.assertEqual(output.getvalue().count("kind=fragment"), 1)

    def test_finish_current_trace_closes_fragment_and_parent_before_rerun(self):
        output = self.capture_logs()
        parent = start_trace(True, "full_rerun", "app")
        start_trace(True, "fragment", "nested")

        summary = finish_current_trace()

        self.assertTrue(parent.finished)
        self.assertEqual(summary["kind"], "full_rerun")
        visible = output.getvalue()
        self.assertEqual(visible.count("kind=fragment name=nested"), 1)
        self.assertEqual(visible.count("kind=full_rerun name=app"), 1)


if __name__ == "__main__":
    unittest.main()
