import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from functools import wraps
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import Settings
from db import Database, initialize_postgres_schema
from dev_memory_db import DevMemoryDatabase, new_dev_local_store
from dev_tools import delete_dev_case, seed_dev_case


ARTIFACT_KINDS = (
    "DISPUTE_MAP",
    "FINAL_JUDGMENT",
    "JUDGMENT_NORMAL",
    "JUDGMENT_SWAPPED",
    "META_JUDGMENT",
)


class CallRecorder:
    def __init__(self, database):
        self.database = database
        self.calls = []

    def clear(self):
        self.calls.clear()

    def __getattr__(self, name):
        attribute = getattr(self.database, name)
        if name.startswith("_") or not callable(attribute):
            return attribute

        @wraps(attribute)
        def measured(*args, **kwargs):
            started = time.perf_counter()
            try:
                return attribute(*args, **kwargs)
            finally:
                self.calls.append(
                    (name, (time.perf_counter() - started) * 1000)
                )

        return measured


def settings_for(mode, database_url=""):
    return Settings(
        database_url=database_url,
        llm_endpoint="",
        llm_model="",
        llm_api_key="",
        admin_create_secret="",
        admin_console_route_key="",
        admin_maintenance_secret="",
        dev_mode=True,
        llm_mode="mock",
        dev_database_mode="local" if mode == "fast" else "postgres",
    )


def render_common_before(database, case_id, role, selected_tab):
    database.get_case(case_id)
    database.get_unread_notifications(case_id, role)
    database.get_case_overview(case_id)
    if selected_tab == "statement":
        database.get_statement(case_id, role)
    elif selected_tab == "mediation":
        database.get_mediation_snapshot(case_id, 0)
    elif selected_tab == "final":
        database.get_case(case_id)
        for kind in ARTIFACT_KINDS:
            database.get_artifact(case_id, kind)
        database.get_arbitration_evidence(case_id)
        database.get_case(case_id)
        database.get_artifact(case_id, "FINAL_JUDGMENT")
    else:
        raise ValueError("unknown selected tab")


def render_common_after(database, case_id, role, selected_tab):
    database.get_case_view_snapshot(case_id, role, selected_tab, 0)


def render_common(database, case_id, role, selected_tab, architecture):
    renderer = (
        render_common_before
        if architecture == "before"
        else render_common_after
    )
    renderer(database, case_id, role, selected_tab)


def measure(recorder, action):
    recorder.clear()
    started = time.perf_counter()
    action()
    total_ms = (time.perf_counter() - started) * 1000
    return {
        "total_ms": total_ms,
        "db_calls": len(recorder.calls),
        "db_total_ms": sum(duration for _, duration in recorder.calls),
        "calls": list(recorder.calls),
    }


def summarize(samples):
    methods = Counter()
    durations = defaultdict(list)
    for sample in samples:
        for method, duration in sample["calls"]:
            methods[method] += 1
            durations[method].append(duration)
    slowest_method = max(
        durations,
        key=lambda method: max(durations[method]),
        default="none",
    )
    return {
        "samples": len(samples),
        "total_median_ms": statistics.median(
            sample["total_ms"] for sample in samples
        ),
        "db_calls_median": statistics.median(
            sample["db_calls"] for sample in samples
        ),
        "db_total_median_ms": statistics.median(
            sample["db_total_ms"] for sample in samples
        ),
        "slowest_db_method": slowest_method,
        "slowest_db_max_ms": (
            max(durations[slowest_method]) if durations else 0.0
        ),
        "method_calls_per_sample": {
            method: count / len(samples)
            for method, count in sorted(methods.items())
        },
        "method_median_ms": {
            method: statistics.median(values)
            for method, values in sorted(durations.items())
        },
        "llm_calls": 0,
    }


def benchmark(database, settings, samples, architecture):
    recorder = CallRecorder(database)
    created = []

    def seed(scenario):
        dev_case = seed_dev_case(
            settings,
            database,
            "weekend_plan",
            scenario,
        )
        created.append(dev_case.case_id)
        return dev_case.case_id

    results = defaultdict(list)
    try:
        case_id = seed("MEDIATING")
        for _ in range(samples):
            results["Enter Case"].append(
                measure(
                    recorder,
                    lambda: render_common(
                        recorder,
                        case_id,
                        "A",
                        "statement",
                        architecture,
                    ),
                )
            )
            results["Send Message"].append(
                measure(
                    recorder,
                    lambda: send_message(
                        recorder,
                        case_id,
                        architecture,
                    ),
                )
            )
            results["Passive Poll"].append(
                measure(
                    recorder,
                    lambda: passive_poll(
                        recorder,
                        case_id,
                        architecture,
                    ),
                )
            )

        case_id = seed("MEDIATING")
        for _ in range(samples):
            results["Pause"].append(
                measure(
                    recorder,
                    lambda: (
                        recorder.pause_case(case_id, "A"),
                        render_common(
                            recorder,
                            case_id,
                            "A",
                            "mediation",
                            architecture,
                        ),
                    ),
                )
            )
            recorder.resume_case(case_id, "A")

        case_id = seed("PAUSED")
        for _ in range(samples):
            results["Resume"].append(
                measure(
                    recorder,
                    lambda: (
                        recorder.resume_case(case_id, "B"),
                        render_common(
                            recorder,
                            case_id,
                            "B",
                            "mediation",
                            architecture,
                        ),
                    ),
                )
            )
            recorder.pause_case(case_id, "B")

        case_id = seed("MEDIATING")
        for _ in range(samples):
            results["Arbitration Request"].append(
                measure(
                    recorder,
                    lambda: (
                        recorder.request_arbitration(case_id, "A"),
                        render_common(
                            recorder,
                            case_id,
                            "A",
                            "final",
                            architecture,
                        ),
                    ),
                )
            )
            recorder.cancel_arbitration_request(case_id, "A")

        case_id = seed("ARBITRATION_PENDING_A")
        for _ in range(samples):
            results["Arbitration Decline"].append(
                measure(
                    recorder,
                    lambda: (
                        recorder.cancel_arbitration_request(case_id, "B"),
                        render_common(
                            recorder,
                            case_id,
                            "B",
                            "final",
                            architecture,
                        ),
                    ),
                )
            )
            recorder.request_arbitration(case_id, "A")

        case_id = seed("ARBITRATION_PENDING_A")
        for _ in range(samples):
            recorder.cancel_arbitration_request(case_id, "B")
            notification = recorder.get_unread_notifications(case_id, "A")[-1]
            results["Ack Notification"].append(
                measure(
                    recorder,
                    lambda: (
                        recorder.mark_notification_read(
                            case_id,
                            notification["id"],
                            "A",
                        ),
                        render_common(
                            recorder,
                            case_id,
                            "A",
                            "final",
                            architecture,
                        ),
                    ),
                )
            )
            recorder.request_arbitration(case_id, "A")
    finally:
        for case_id in reversed(created):
            delete_dev_case(settings, database, case_id)

    return {action: summarize(values) for action, values in results.items()}


def send_message(database, case_id, architecture):
    database.add_message(case_id, "A", "benchmark message")
    if architecture == "before":
        database.get_mediation_snapshot(case_id, 0)


def passive_poll(database, case_id, architecture):
    if architecture == "before":
        database.get_unread_notifications(case_id, "A")
        database.get_case_overview(case_id)
        database.get_mediation_snapshot(case_id, 0)
    else:
        database.get_case_revision(case_id, "A")


def create_database(mode, test_database_url):
    if mode == "fast":
        settings = settings_for(mode)
        store = new_dev_local_store(settings)
        return (
            DevMemoryDatabase(settings, store, database_mode="local"),
            settings,
            {},
            None,
        )

    if not test_database_url:
        raise RuntimeError("TEST_DATABASE_URL is required for integration mode")
    settings = settings_for(mode, test_database_url)
    started = time.perf_counter()
    pool = Database.create_pool(test_database_url)
    pool_create_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    initialize_postgres_schema(pool)
    schema_check_ms = (time.perf_counter() - started) * 1000
    checkout_samples = []
    query_samples = []
    for _ in range(5):
        started = time.perf_counter()
        with pool.connection():
            pass
        checkout_samples.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        with pool.connection() as connection:
            connection.execute("SELECT 1").fetchone()
        query_samples.append((time.perf_counter() - started) * 1000)
    pool_metrics = {
        "pool_create_cold_ms": pool_create_ms,
        "schema_check_ms": schema_check_ms,
        "warm_checkout_median_ms": statistics.median(checkout_samples),
        "warm_select_one_median_ms": statistics.median(query_samples),
        "pool_min_size": 2,
        "pool_max_size": 5,
        "pool_timeout_seconds": 10,
    }
    return Database(pool=pool), settings, pool_metrics, pool


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("fast", "integration"), required=True)
    parser.add_argument(
        "--architecture",
        choices=("before", "after"),
        required=True,
    )
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")

    import os

    database, settings, pool_metrics, pool = create_database(
        args.mode,
        os.environ.get("TEST_DATABASE_URL", ""),
    )
    try:
        actions = benchmark(
            database,
            settings,
            args.samples,
            args.architecture,
        )
    finally:
        if pool is not None:
            pool.close()
    report = {
        "environment": "DEV_FAST" if args.mode == "fast" else "DEV_INTEGRATION",
        "architecture": args.architecture,
        "samples": args.samples,
        "pool": pool_metrics,
        "actions": actions,
        "hidden_tab_db_calls": 0,
        "polling_interval_seconds": 2 if args.mode == "integration" else None,
        "polling_db_calls_per_minute": (
            (90 if args.architecture == "before" else 30)
            if args.mode == "integration"
            else 0
        ),
        "real_llm_calls": 0,
        "production_writes": 0,
    }
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
