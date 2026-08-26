import inspect
import logging
import re
import secrets
import statistics
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps


logger = logging.getLogger("performance")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.:<>=-]")
_current_trace = ContextVar("performance_trace", default=None)


def _safe_name(value):
    return _SAFE_NAME.sub("_", str(value))[:80]


def _caller_name():
    frame = inspect.currentframe()
    try:
        caller = frame.f_back.f_back if frame and frame.f_back else None
        return _safe_name(caller.f_code.co_name if caller else "unknown")
    finally:
        del frame


@dataclass(frozen=True)
class CallTiming:
    name: str
    caller: str
    duration_ms: float


@dataclass
class PerformanceTrace:
    kind: str
    name: str
    parent: "PerformanceTrace | None" = None
    trace_id: str = field(default_factory=lambda: secrets.token_hex(4))
    started_at: float = field(default_factory=time.perf_counter)
    database_calls: list[CallTiming] = field(default_factory=list)
    llm_calls: list[CallTiming] = field(default_factory=list)
    finished: bool = False
    token: object = field(default=None, repr=False)

    def record_database(self, name, duration_ms, caller):
        self.database_calls.append(
            CallTiming(_safe_name(name), _safe_name(caller), duration_ms)
        )

    def record_llm(self, name, duration_ms, caller):
        self.llm_calls.append(
            CallTiming(_safe_name(name), _safe_name(caller), duration_ms)
        )

    def finish(self):
        if self.finished:
            return None
        self.finished = True
        total_ms = (time.perf_counter() - self.started_at) * 1000
        db_total_ms = sum(call.duration_ms for call in self.database_calls)
        llm_total_ms = sum(call.duration_ms for call in self.llm_calls)
        render_ms = max(0.0, total_ms - db_total_ms - llm_total_ms)
        slowest = max(
            self.database_calls,
            key=lambda call: call.duration_ms,
            default=None,
        )

        logger.warning(
            "PERF trace=%s kind=%s name=%s total_ms=%.2f "
            "db_calls=%d db_total_ms=%.2f slowest_db_method=%s "
            "slowest_db_ms=%.2f llm_calls=%d llm_total_ms=%.2f "
            "render_ms=%.2f",
            self.trace_id,
            _safe_name(self.kind),
            _safe_name(self.name),
            total_ms,
            len(self.database_calls),
            db_total_ms,
            slowest.name if slowest else "none",
            slowest.duration_ms if slowest else 0.0,
            len(self.llm_calls),
            llm_total_ms,
            render_ms,
        )
        self._log_database_aggregates()

        if self.parent is not None:
            self.parent.database_calls.extend(self.database_calls)
            self.parent.llm_calls.extend(self.llm_calls)
        if _current_trace.get() is self and self.token is not None:
            _current_trace.reset(self.token)
        return {
            "trace_id": self.trace_id,
            "kind": self.kind,
            "name": self.name,
            "total_ms": total_ms,
            "db_calls": len(self.database_calls),
            "db_total_ms": db_total_ms,
            "llm_calls": len(self.llm_calls),
            "llm_total_ms": llm_total_ms,
            "render_ms": render_ms,
        }

    def _log_database_aggregates(self):
        grouped = {}
        call_graph = {}
        for call in self.database_calls:
            grouped.setdefault(call.name, []).append(call.duration_ms)
            call_graph[(call.name, call.caller)] = (
                call_graph.get((call.name, call.caller), 0) + 1
            )
        for method in sorted(grouped):
            durations = grouped[method]
            logger.warning(
                "PERF trace=%s db_method=%s calls=%d total_ms=%.2f "
                "median_ms=%.2f max_ms=%.2f",
                self.trace_id,
                method,
                len(durations),
                sum(durations),
                statistics.median(durations),
                max(durations),
            )
        for (method, caller), count in sorted(call_graph.items()):
            logger.warning(
                "PERF trace=%s db_call_graph method=%s caller=%s calls=%d",
                self.trace_id,
                method,
                caller,
                count,
            )


def start_trace(enabled, kind, name):
    if not enabled:
        return None
    parent = _current_trace.get()
    trace = PerformanceTrace(
        kind=_safe_name(kind),
        name=_safe_name(name),
        parent=parent,
    )
    trace.token = _current_trace.set(trace)
    return trace


def finish_trace(trace):
    return trace.finish() if trace is not None else None


def finish_current_trace():
    trace = _current_trace.get()
    summary = None
    while trace is not None:
        parent = trace.parent
        summary = finish_trace(trace)
        trace = parent
    return summary


def record_database_call(name, duration_ms, caller=None):
    trace = _current_trace.get()
    if trace is not None:
        trace.record_database(name, duration_ms, caller or _caller_name())


def record_llm_call(name, duration_ms, caller=None):
    trace = _current_trace.get()
    if trace is not None:
        trace.record_llm(name, duration_ms, caller or _caller_name())


class InstrumentedDatabase:
    def __init__(self, database):
        self._database = database

    def __getattr__(self, name):
        attribute = getattr(self._database, name)
        if name.startswith("_") or not callable(attribute):
            return attribute

        @wraps(attribute)
        def measured(*args, **kwargs):
            started = time.perf_counter()
            caller = _caller_name()
            try:
                return attribute(*args, **kwargs)
            finally:
                record_database_call(
                    name,
                    (time.perf_counter() - started) * 1000,
                    caller,
                )

        return measured


def instrument_database(database, enabled):
    if not enabled or isinstance(database, InstrumentedDatabase):
        return database
    return InstrumentedDatabase(database)


def observe_fragment(name, enabled):
    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            active = enabled() if callable(enabled) else enabled
            trace = start_trace(active, "fragment", name)
            try:
                return function(*args, **kwargs)
            finally:
                finish_trace(trace)

        return wrapped

    return decorator
