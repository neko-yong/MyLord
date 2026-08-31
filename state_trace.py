"""Opt-in, payload-free lifecycle tracing. Never accepts arbitrary log text."""
import hashlib
import json
import logging
import re
import secrets
import time
from contextvars import ContextVar
from functools import wraps


logger = logging.getLogger("state_trace")
_current = ContextVar("state_trace", default=None)
STATUSES = {
    "COLLECTING", "READY_FOR_MAP", "MAP_READY", "MEDIATING", "PAUSED",
    "ARBITRATION_PENDING", "ARBITRATING", "CLOSED", "unknown",
}
VIEWS = {"statement", "dispute", "mediation", "final", "none", "invalid"}
REASONS = {
    "initial_or_widget", "tab_change", "explicit", "revision_changed",
    "case_sync", "mediation_room", "confirmation_dialog", "notification_dialog",
}
EVENTS = {
    "run_start", "run_exit", "stop", "rerun_requested", "button_click",
    "snapshot_started", "snapshot_refreshed", "snapshot_failed", "snapshot_missing",
    "tabs_registered", "render_branch_entered", "render_complete",
    "poll_started", "poll_finished", "poll_failed", "poll_skipped",
    "db_request_started", "db_request_finished", "evidence_freeze_started",
    "evidence_freeze_finished", "state_transition", "llm_started", "llm_finished",
    "llm_failed", "artifact_persist_started", "artifact_persisted",
    "notification_ack", "action_failed",
}
DEFAULTS = {
    "case_status": "unknown", "arbitration_state": "unknown",
    "selected_tab": "none", "selected_tab_open_flags": "unknown",
    "authenticated": False, "case_id_present": False, "role_present": False,
    "snapshot_present": False, "revision_before": "none", "revision_after": "none",
    "llm_started": False, "llm_finished": False, "render_branch": "none",
    "requested_scope": "none",
}


def revision_fingerprint(revision):
    if revision is None:
        return "none"
    return hashlib.sha256(json.dumps(
        revision, sort_keys=True, default=str, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:12]


def _safe_field(name, value):
    if isinstance(DEFAULTS[name], bool):
        return value if isinstance(value, bool) else False
    if name in {"case_status", "arbitration_state"}:
        allowed = STATUSES
    elif name in {"selected_tab", "render_branch"}:
        allowed = VIEWS
    elif name == "requested_scope":
        allowed = {"app", "fragment", "none"}
    elif name == "selected_tab_open_flags":
        return value if isinstance(value, str) and re.fullmatch("[01]{4}", value) else "unknown"
    else:
        return value if isinstance(value, str) and re.fullmatch("[0-9a-f]{12}|none", value) else "none"
    return value if isinstance(value, str) and value in allowed else "invalid"


class StateTrace:
    def __init__(self, scope, reason, fields):
        self.trace_id = secrets.token_hex(4)
        self.scope = scope if scope in {"app", "fragment"} else "unknown"
        self.reason = reason if reason in REASONS else "initial_or_widget"
        self.fields = dict(DEFAULTS)
        self.started = time.perf_counter()
        self.token = _current.set(self)
        self.closed = False
        self.emit("run_start", **fields)

    def emit(self, event, **fields):
        if self.closed:
            return
        for name, value in fields.items():
            if name in DEFAULTS:
                self.fields[name] = _safe_field(name, value)
        row = dict(self.fields, trace_id=self.trace_id, rerun_scope=self.scope,
                   rerun_reason=self.reason, event=event if event in EVENTS else "invalid",
                   elapsed_ms=round((time.perf_counter() - self.started) * 1000, 2))
        logger.warning("STATE %s", json.dumps(row, sort_keys=True))

    def finish(self):
        if not self.closed:
            self.emit("run_exit")
            self.closed = True
            _current.reset(self.token)


def start(enabled, scope, reason, **fields):
    return StateTrace(scope, reason, fields) if enabled else None


def event(name, **fields):
    trace = _current.get()
    if trace is not None:
        trace.emit(name, **fields)


def finish(trace):
    if trace is not None:
        trace.finish()


def finish_current():
    while _current.get() is not None:
        finish(_current.get())


def observe_fragment(name, enabled, state):
    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            active = enabled()
            parent = _current.get()
            trace = start(active, parent.scope if parent else "fragment", name,
                          **state() if active else {})
            try:
                return function(*args, **kwargs)
            finally:
                finish(trace)
        return wrapped
    return decorator
