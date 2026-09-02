import io
import logging
import unittest

import state_trace as trace


class StateTraceTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        handler = logging.StreamHandler(self.output)
        trace.logger.addHandler(handler)
        self.addCleanup(trace.logger.removeHandler, handler)
        self.addCleanup(trace.finish_current)

    def test_disabled_trace_emits_nothing(self):
        self.assertIsNone(trace.start(False, "app", "initial_or_widget"))
        for event in trace.EVENTS:
            trace.event(event, case_status="MEDIATING")
        self.assertEqual(self.output.getvalue(), "")

    def test_live_events_are_written_before_operation_finishes(self):
        current = trace.start(True, "app", "tab_change", authenticated=True,
                              selected_tab="mediation", selected_tab_open_flags="0010")
        trace.event("llm_started", llm_started=True)
        self.assertIn('"event": "llm_started"', self.output.getvalue())
        self.assertNotIn('"event": "run_exit"', self.output.getvalue())
        trace.finish(current)
        self.assertIn('"event": "run_exit"', self.output.getvalue())

    def test_allowlists_reject_private_values_even_in_permitted_fields(self):
        canary = "A-private-token postgres://secret/private-body"
        current = trace.start(True, canary, canary)
        trace.event(canary, **{key: canary for key in trace.DEFAULTS}, raw_token=canary)
        trace.event("snapshot_refreshed", revision_after=trace.revision_fingerprint({"x": canary}))
        trace.finish(current)
        self.assertNotIn(canary, self.output.getvalue())
        self.assertNotIn("raw_token", self.output.getvalue())
        for key in trace.DEFAULTS:
            self.assertIn(f'"{key}"', self.output.getvalue())

    def test_phase_fields_accept_only_bounded_sanitized_values(self):
        current = trace.start(
            True,
            "app",
            "confirmation_complete",
            confirmation_action="pause",
            pending_confirmation=True,
            phase_outcome="enter",
            run_sequence=7,
        )
        trace.event(
            "pending_cleared",
            confirmation_action="private-action",
            pending_confirmation=False,
            phase_outcome="private-result",
            run_sequence="private-sequence",
        )
        trace.finish(current)
        output = self.output.getvalue()
        self.assertIn('"confirmation_action": "invalid"', output)
        self.assertIn('"pending_confirmation": false', output)
        self.assertIn('"phase_outcome": "invalid"', output)
        self.assertIn('"run_sequence": 0', output)
        self.assertNotIn("private-action", output)

    def test_fragment_scope_and_parent_are_restored_on_exception(self):
        @trace.observe_fragment("case_sync", lambda: True, lambda: {"authenticated": True})
        def fragment():
            trace.event("poll_started")
            raise RuntimeError("private exception details must not be logged")

        with self.assertRaises(RuntimeError):
            fragment()
        self.assertIn('"rerun_scope": "fragment"', self.output.getvalue())
        self.assertNotIn("private exception", self.output.getvalue())
        current = trace.start(True, "app", "initial_or_widget")
        with self.assertRaises(RuntimeError):
            fragment()
        trace.event("render_complete")
        trace.finish(current)
        self.assertIn('"event": "render_complete"', self.output.getvalue())
        before = self.output.getvalue()
        trace.event("poll_started")
        self.assertEqual(before, self.output.getvalue())

    def test_rerun_closes_nested_contexts_once(self):
        parent = trace.start(True, "app", "initial_or_widget")
        child = trace.start(True, "fragment", "confirmation_dialog")
        trace.event("rerun_requested")
        trace.finish_current()
        trace.finish(child)
        trace.finish(parent)
        self.assertEqual(self.output.getvalue().count('"event": "run_exit"'), 2)


if __name__ == "__main__":
    unittest.main()
