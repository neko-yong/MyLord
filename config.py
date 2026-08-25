import os
import secrets
from dataclasses import dataclass
from typing import Mapping, Any


@dataclass(frozen=True)
class Settings:
    database_url: str
    llm_endpoint: str
    llm_model: str
    llm_api_key: str
    admin_create_secret: str
    development_mode: bool = False

    @property
    def llm_ready(self):
        return all((self.llm_endpoint, self.llm_model, self.llm_api_key))

    @property
    def admin_create_ready(self):
        return bool(self.admin_create_secret)


def _value(name, secrets_values, environ):
    value = secrets_values.get(name)
    if value is None or str(value).strip() == "":
        value = environ.get(name, "")
    return str(value).strip()


def _as_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def secure_secret_matches(candidate, expected):
    if not candidate or not expected:
        return False
    return secrets.compare_digest(
        candidate.encode("utf-8"),
        expected.encode("utf-8"),
    )


def load_settings(
    secrets_values: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
):
    secrets_values = secrets_values or {}
    environ = environ or os.environ
    return Settings(
        database_url=_value("DATABASE_URL", secrets_values, environ),
        llm_endpoint=_value("LLM_ENDPOINT", secrets_values, environ),
        llm_model=_value("LLM_MODEL", secrets_values, environ),
        llm_api_key=_value("LLM_API_KEY", secrets_values, environ),
        admin_create_secret=_value("ADMIN_CREATE_SECRET", secrets_values, environ),
        development_mode=_as_bool(
            _value("DEVELOPMENT_MODE", secrets_values, environ)
        ),
    )
