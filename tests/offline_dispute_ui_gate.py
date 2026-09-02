"""Run only synthetic tests behind process-wide offline guards.

Invoke with the existing venv Python using -I -B; never run app.py directly.
No secrets, test output, exception messages or tracebacks are printed.
"""

from collections import Counter
import compileall
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import inspect
import io
import json
import os
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BLOCKED = Counter()
MODULES = (
    "test_dispute_map_view", "test_app_dispute_map_context",
    "test_plain_language_prompts", "test_arbitration", "test_evidence",
    "test_llm", "test_mock_llm", "test_dev_fixtures", "test_dev_tools",
    "test_statement_validation", "test_app_auto_dispute_map",
    "test_app_arbitration_ui", "test_app_chat_fragment",
    "test_app_dev_playground", "test_app_missing_database",
    "test_app_statement_validation", "test_app_state_trace",
    "test_confirmation_rerun_order", "test_state_trace", "test_config",
    "test_secret_redaction", "test_db_security", "test_database_resources",
    "test_dev_memory_db", "test_admin_layout_stability",
    "test_perf_admin_integration", "test_admin_console", "test_performance",
    "test_admin_delete_confirmation", "test_admin_database",
    "test_database_state_machine", "test_integration_config",
)


def refuse(label):
    def denied(*_args, **_kwargs):
        BLOCKED[label] += 1
        raise RuntimeError("Offline guard blocked access")
    return denied


def audit(event, args):
    if event in {
        "socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
        "socket.gethostbyaddr", "socket.getnameinfo", "socket.bind",
        "socket.sendto", "socket.sendmsg", "subprocess.Popen", "os.system",
        "os.posix_spawn",
    }:
        refuse(event)()
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        name = os.fsdecode(args[0]).replace("\\", "/").lower()
        basename = name.rsplit("/", 1)[-1]
        if ("/.streamlit/" in name or basename.startswith(".env")
                or basename in {"secrets.toml", ".pgpass", "pgpass.conf", "pg_service.conf"}):
            refuse("configuration_file")()


def main():
    # Allowlist OS runtime paths only; do not retain any application environment.
    runtime_paths = {key: os.environ[key] for key in (
        "SYSTEMROOT", "WINDIR", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "TEMP", "TMP", "PATH",
    ) if key in os.environ}
    os.environ.clear()
    os.environ.update(runtime_paths)
    os.chdir(ROOT)
    sys.addaudithook(audit)
    stack = ExitStack()  # Intentionally remains active until process exit.
    version = sys.getwindowsversion()
    stack.enter_context(patch("platform.win32_ver", return_value=(
        str(version.major), f"{version.major}.{version.minor}.{version.build}", "", "",
    )))
    stack.enter_context(patch("streamlit.config.get_config_files", return_value=[]))
    stack.enter_context(patch(
        "streamlit.runtime.secrets.Secrets.load_if_toml_exists", return_value=False,
    ))
    guards = (
        ("streamlit.runtime.secrets.Secrets._parse", "secrets_parse"),
        ("requests.sessions.Session.request", "http"),
        ("http.client.HTTPConnection.connect", "http_connect"),
        ("psycopg.connect", "postgres_connect"),
        ("psycopg.Connection.connect", "postgres_connection"),
        ("psycopg.AsyncConnection.connect", "postgres_async_connection"),
        ("psycopg_pool.ConnectionPool.__init__", "postgres_pool"),
        ("psycopg_pool.AsyncConnectionPool.__init__", "postgres_async_pool"),
    )
    guarded = [stack.enter_context(patch(target, new=refuse(label)))
               for target, label in guards]
    bootstrap = dict(BLOCKED)
    # urllib3 may probe IPv6 availability; the bind is denied, even on loopback.
    assert set(bootstrap).issubset({"socket.bind"})
    BLOCKED.clear()
    import socket
    import subprocess
    probes = [lambda guard=guard: guard() for guard in guarded]
    probes += [
        lambda: socket.getaddrinfo("offline.invalid", 443),
        lambda: socket.socket().connect(("127.0.0.1", 9)),
        lambda: open(ROOT / "offline_canary" / "secrets.toml"),
        lambda: open(ROOT / "offline_canary" / ".env.local"),
        lambda: subprocess.Popen([sys.executable, "-c", "pass"]),
    ]
    for probe in probes:
        before = sum(BLOCKED.values())
        try:
            probe()
        except RuntimeError:
            pass
        else:
            raise AssertionError("Isolation self-test did not block")
        assert sum(BLOCKED.values()) == before + 1
    BLOCKED.clear()

    import streamlit as st
    assert st.__version__ == "1.62.0"
    assert "wrap" in inspect.signature(st.columns).parameters
    assert "border" in inspect.signature(st.container).parameters
    assert inspect.signature(st.expander).parameters["on_change"].default == "ignore"
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    package = types.ModuleType("tests")
    package.__path__ = [str(ROOT / "tests")]
    package.__package__ = "tests"
    sys.modules["tests"] = package
    compilation = all(compileall.compile_file(str(path), quiet=2)
                      for path in sorted(ROOT.rglob("*.py")))
    records = []
    selected = sys.argv[1:] or MODULES
    assert all(name in MODULES for name in selected)
    for name in selected:
        suite = unittest.defaultTestLoader.loadTestsFromName("tests." + name)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        records.append({
            "module": name, "run": result.testsRun,
            "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped),
            "failed_ids": [test.id() for test, _ in result.failures + result.errors],
            "failure_locations": [
                {"file": Path(filename).name, "line": int(line)}
                for _, diagnostic in result.failures + result.errors
                for filename, line in re.findall(r'File "([^"]+)", line (\d+)', diagnostic)
            ],
        })
        if BLOCKED:
            break
    ok = (compilation and len(records) == len(selected) and not BLOCKED
          and not any(row["failures"] or row["errors"] or row["skipped"] for row in records))
    return {
        "status": "PASS" if ok else "FAIL", "streamlit": st.__version__,
        "isolation_self_tests": len(probes), "bootstrap_blocked": bootstrap,
        "compileall": compilation, "modules": records,
        "run": sum(row["run"] for row in records),
        "failures": sum(row["failures"] for row in records),
        "errors": sum(row["errors"] for row in records),
        "skipped": sum(row["skipped"] for row in records),
        "forbidden_access_attempts_during_tests": dict(BLOCKED),
    }


if __name__ == "__main__":
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            summary = main()
    except Exception as error:
        frame = error.__traceback__
        frames = []
        while frame:
            frames.append({"file": Path(frame.tb_frame.f_code.co_filename).name,
                           "function": frame.tb_frame.f_code.co_name,
                           "line": frame.tb_lineno})
            frame = frame.tb_next
        summary = {"status": "FAIL", "exception_type": type(error).__name__,
                   "frames": frames, "blocked": dict(BLOCKED)}
    print(json.dumps(summary, ensure_ascii=True))
    raise SystemExit(0 if summary["status"] == "PASS" else 1)
