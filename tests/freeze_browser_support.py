"""Loopback-only browser fixture: production UI, memory DB, synthetic data."""
import logging
import json
import os
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import streamlit as st

import config
import database_resources
import llm
from db import DatabaseUnavailable, hash_token
from dev_memory_db import DevMemoryDatabase, new_dev_local_store
from tests.test_dispute_map_view import fictional_map


ROOT = Path(__file__).resolve().parents[1]
BASELINE = subprocess.check_output(
    [
        "git",
        "-c",
        f"safe.directory={ROOT}",
        "show",
        "bef2e47d58a018c2ccf87458a5ff4a9fe38be2d1:app.py",
    ],
    cwd=ROOT,
).decode("utf-8")
LOCAL = SimpleNamespace(dev_mode=True, dev_database_mode="local")
STORE = new_dev_local_store(LOCAL)
MEMORY = DevMemoryDatabase(LOCAL, STORE)
CASE_ID, _, _ = MEMORY.create_case("[DEV_TEST] Freeze browser gate")
STORE["cases"][CASE_ID]["a_token_hash"] = hash_token("A-browser-fixture")
STORE["cases"][CASE_ID]["b_token_hash"] = hash_token("B-browser-fixture")
if os.environ.get("FREEZE_GATE_START") != "collecting":
    MEMORY.save_statement(CASE_ID, "A", "Synthetic A statement for browser gate")
    MEMORY.save_statement(CASE_ID, "B", "Synthetic B statement for browser gate")
    reservation = MEMORY.claim_artifact(CASE_ID, "DISPUTE_MAP")
    MEMORY.complete_artifact(CASE_ID, reservation, "DISPUTE_MAP", fictional_map())
CALLS = Counter()
LOCK = threading.RLock()
RESET_STATE = {"nonce": None, "scenario": "map_ready", "seen": set()}
LOG = logging.getLogger("freeze_browser_gate")
LOG.setLevel(logging.WARNING)
DELAYS = json.loads(os.environ.get("FREEZE_GATE_DELAYS", "{}"))
AFTER_DELAYS = json.loads(os.environ.get("FREEZE_GATE_AFTER_DELAYS", "{}"))
FAIL_BEFORE = json.loads(os.environ.get("FREEZE_GATE_FAIL_BEFORE", "{}"))
FAIL_AFTER = json.loads(os.environ.get("FREEZE_GATE_FAIL_AFTER", "{}"))
ARTIFACT_DIR = ROOT / "tests" / "artifacts"
ARTIFACT_DIR.mkdir(exist_ok=True)
handler = logging.FileHandler(ARTIFACT_DIR / f"freeze-{os.getpid()}.log", encoding="utf-8")
handler.setFormatter(logging.Formatter("%(created).6f thread=%(thread)d %(message)s"))
for name in ("freeze_browser_gate", "state_trace", "performance"):
    logging.getLogger(name).addHandler(handler)


def reset_fixture(scenario, nonce):
    """Reset only this loopback fixture, once for all sessions sharing a nonce."""
    global CASE_ID
    if scenario not in {"statement", "pause", "arbitration_accept"}:
        raise ValueError("Unsupported local freeze-gate scenario")
    if not isinstance(nonce, str) or not nonce.isascii() or not nonce.isalnum():
        raise ValueError("Invalid local freeze-gate nonce")
    with LOCK:
        if nonce in RESET_STATE["seen"]:
            return CASE_ID
        MEMORY.reset()
        CASE_ID, _, _ = MEMORY.create_case("[DEV_TEST] Freeze confirmation gate")
        STORE["cases"][CASE_ID]["a_token_hash"] = hash_token("A-browser-fixture")
        STORE["cases"][CASE_ID]["b_token_hash"] = hash_token("B-browser-fixture")
        if scenario in {"pause", "arbitration_accept"}:
            MEMORY.save_statement(CASE_ID, "A", "Synthetic A statement for confirmation gate")
            MEMORY.save_statement(CASE_ID, "B", "Synthetic B statement for confirmation gate")
            reservation = MEMORY.claim_artifact(CASE_ID, "DISPUTE_MAP")
            MEMORY.complete_artifact(
                CASE_ID,
                reservation,
                "DISPUTE_MAP",
                fictional_map(),
            )
        if scenario == "arbitration_accept":
            MEMORY.request_arbitration(CASE_ID, "A")
        CALLS.clear()
        RESET_STATE["seen"].add(nonce)
        RESET_STATE.update(nonce=nonce, scenario=scenario)
        LOG.warning("GATE fixture_reset=%s", scenario)
        return CASE_ID


def begin_operation(name, default_delay=0):
    with LOCK:
        CALLS[name] += 1
        ordinal = CALLS[name]
    delays = DELAYS.get(name, [default_delay])
    delay = delays[(ordinal - 1) % len(delays)]
    LOG.warning("GATE start=%s ordinal=%d delay=%.3f", name, ordinal, delay)
    time.sleep(delay)
    if ordinal in FAIL_BEFORE.get(name, []):
        LOG.warning("GATE injected_failure=%s ordinal=%d phase=before", name, ordinal)
        raise DatabaseUnavailable("Synthetic temporary dependency failure")
    return ordinal


class FixtureDatabase:
    def __getattr__(self, name):
        method = getattr(MEMORY, name)

        def call(*args, **kwargs):
            default_delay = float(os.environ.get("FREEZE_GATE_DB_DELAY", "0")) if name in {
                "request_arbitration", "confirm_arbitration"
            } else 0
            ordinal = begin_operation(name, default_delay)
            try:
                with LOCK:
                    result = method(*args, **kwargs)
                delays = AFTER_DELAYS.get(name, [0])
                after_delay = delays[(ordinal - 1) % len(delays)]
                if after_delay:
                    LOG.warning("GATE response_delay=%s ordinal=%d delay=%.3f", name, ordinal, after_delay)
                    time.sleep(after_delay)
                if ordinal in FAIL_AFTER.get(name, []):
                    LOG.warning("GATE injected_failure=%s ordinal=%d phase=after", name, ordinal)
                    raise DatabaseUnavailable("Synthetic response failure after commit")
            except Exception:
                LOG.warning("GATE failed=%s ordinal=%d", name, ordinal)
                raise
            LOG.warning("GATE finish=%s ordinal=%d status=%s", name, ordinal, MEMORY.get_case(CASE_ID)["status"])
            return result

        return call


DATABASE = FixtureDatabase()
SETTINGS = config.Settings(
    database_url="fixture-only",
    llm_endpoint="fixture-only",
    llm_model="fixture-only",
    llm_api_key="fixture-only",
    admin_create_secret="",
    admin_console_route_key="",
    admin_maintenance_secret="",
    development_mode=True,
    perf_debug=True,
)
config.load_settings = lambda *_args, **_kwargs: SETTINGS
database_resources.get_database = lambda *_args: DATABASE
st.secrets.load_if_toml_exists = lambda: False


def mock_call(**_kwargs):
    delay = float(os.environ.get("FREEZE_GATE_LLM_DELAY", "3"))
    try:
        ordinal = begin_operation("mock_llm", delay)
    except DatabaseUnavailable:
        raise llm.LLMError("timeout", "Synthetic model timeout") from None
    LOG.warning("GATE finish=mock_llm ordinal=%d", ordinal)
    return llm.LLMResult(
        content="Synthetic final judgment", model="fixture-mock",
        finish_reason="stop", prompt_tokens=0, completion_tokens=0,
        total_tokens=0, latency_ms=delay * 1000,
    )


llm.call_llm = mock_call
real_tabs = st.tabs
real_rerun = st.rerun
real_markdown = st.markdown


def traced_tabs(*args, **kwargs):
    tabs = real_tabs(*args, **kwargs)
    LOG.warning("GATE tab_flags=%s", "".join("1" if tab.open else "0" for tab in tabs))
    return tabs


def traced_rerun(*args, **kwargs):
    LOG.warning("GATE rerun_requested=%s", kwargs.get("scope", "app"))
    return real_rerun(*args, **kwargs)


def traced_markdown(body, *args, **kwargs):
    branches = {"### 你的独立陈述": "statement", "### 争议地图": "dispute",
                "### 共享调解室": "mediation", "### 最终仲裁": "final"}
    if body in branches:
        LOG.warning("GATE render_branch=%s", branches[body])
    return real_markdown(body, *args, **kwargs)


st.tabs = traced_tabs
st.rerun = traced_rerun
st.markdown = traced_markdown
