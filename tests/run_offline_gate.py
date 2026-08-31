"""Run unit/AppTest discovery with secrets isolated and real sockets forbidden."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

if __name__ == "__main__":
    with (
        patch.dict(os.environ, {
            "DATABASE_URL": "", "TEST_DATABASE_URL": "", "LLM_API_KEY": "",
            "LLM_ENDPOINT": "", "LLM_MODEL": "", "DEV_MODE": "false",
        }),
        patch("streamlit.runtime.secrets.Secrets._parse", return_value={}),
        patch("socket.socket.connect", side_effect=AssertionError("Offline gate forbids network")),
    ):
        suite = unittest.defaultTestLoader.discover(
            str(ROOT / "tests"), pattern=sys.argv[1] if len(sys.argv) > 1 else "test_*.py",
        )
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
