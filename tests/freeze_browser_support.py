"""Loopback-only browser fixture: production UI, memory DB, synthetic data."""
import logging
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
from db import hash_token
from dev_memory_db import DevMemoryDatabase, new_dev_local_store


ROOT = Path(__file__).resolve().parents[1]
BASELINE = subprocess.check_output(
    ["git", "show", "bef2e47d58a018c2ccf87458a5ff4a9fe38be2d1:app.py"],
    cwd=ROOT,
).decode("utf-8")
LOCAL = SimpleNamespace(dev_mode=True, dev_database_mode="local")
STORE = new_dev_local_store(LOCAL)
MEMORY = DevMemoryDatabase(LOCAL, STORE)
CASE_ID, _, _ = MEMORY.create_case("[DEV_TEST] Freeze browser gate")
STORE["cases"][CASE_ID]["a_token_hash"] = hash_token("A-browser-fixture")
STORE["cases"][CASE_ID]["b_token_hash"] = hash_token("B-browser-fixture")
MEMORY.save_statement(CASE_ID, "A", "Synthetic A statement for browser gate")
MEMORY.save_statement(CASE_ID, "B", "Synthetic B statement for browser gate")
reservation = MEMORY.claim_artifact(CASE_ID, "DISPUTE_MAP")
MEMORY.complete_artifact(CASE_ID, reservation, "DISPUTE_MAP", "Synthetic dispute map")
CALLS = Counter()
LOCK = threading.RLock()
LOG = logging.getLogger("freeze_browser_gate")
LOG.setLevel(logging.WARNING)


class FixtureDatabase:
    def __getattr__(self, name):
        method = getattr(MEMORY, name)

        def call(*args, **kwargs):
            LOG.warning("GATE db_start=%s", name)
            if name in {"request_arbitration", "confirm_arbitration"}:
                time.sleep(float(os.environ.get("FREEZE_GATE_DB_DELAY", "0")))
            with LOCK:
                CALLS[name] += 1
                result = method(*args, **kwargs)
            LOG.warning("GATE db_finish=%s status=%s", name, MEMORY.get_case(CASE_ID)["status"])
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
    perf_debug=True,
)
config.load_settings = lambda *_args, **_kwargs: SETTINGS
database_resources.get_database = lambda *_args: DATABASE
st.secrets.load_if_toml_exists = lambda: False


def mock_call(**_kwargs):
    with LOCK:
        CALLS["mock_llm"] += 1
    LOG.warning("GATE llm_start")
    delay = float(os.environ.get("FREEZE_GATE_LLM_DELAY", "3"))
    time.sleep(delay)
    LOG.warning("GATE llm_finish")
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
