import os

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError


def load_test_database_url():
    environment_value = os.getenv("TEST_DATABASE_URL", "").strip()
    if environment_value:
        return environment_value, "ENV"

    try:
        secret_value = st.secrets.get("TEST_DATABASE_URL")
    except StreamlitSecretNotFoundError:
        return None, "NONE"

    if isinstance(secret_value, str) and secret_value.strip():
        return secret_value.strip(), "STREAMLIT_SECRETS"
    return None, "NONE"
