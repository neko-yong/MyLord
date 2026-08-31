"""Run with streamlit on loopback, never as a deployment entrypoint."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import streamlit as st
import freeze_browser_support as fixture

st.caption(f"LOCAL FIXTURE ONLY — Case ID: {fixture.CASE_ID}")
source = fixture.BASELINE if st.query_params.get("baseline") == "1" else (ROOT / "app.py").read_text(encoding="utf-8")
exec(compile(source, str(ROOT / "app.py"), "exec"), globals())
