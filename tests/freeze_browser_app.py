"""Run with streamlit on loopback, never as a deployment entrypoint."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import streamlit as st
import freeze_browser_support as fixture

scenario = st.query_params.get("fixture_scenario")
nonce = st.query_params.get("fixture_nonce")
if scenario and nonce:
    fixture.reset_fixture(scenario, nonce)
st.caption(f"LOCAL FIXTURE ONLY — Case ID: {fixture.CASE_ID}")
source = fixture.BASELINE if st.query_params.get("baseline") == "1" else (ROOT / "app.py").read_text(encoding="utf-8")
fixture.LOG.warning("GATE ui_run=start")
try:
    exec(compile(source, str(ROOT / "app.py"), "exec"), globals())
except Exception:
    fixture.LOG.warning("GATE ui_run=failed")
    raise
except BaseException:
    fixture.LOG.warning("GATE ui_run=interrupted")
    raise
else:
    st.caption(
        "LOCAL COUNTS — "
        f"save_statement={fixture.CALLS['save_statement']} "
        f"pause_case={fixture.CALLS['pause_case']} "
        f"confirm_arbitration={fixture.CALLS['confirm_arbitration']} "
        f"mock_llm={fixture.CALLS['mock_llm']}"
    )
    fixture.LOG.warning("GATE ui_run=complete")
