import os
import secrets
from dataclasses import dataclass, field
from typing import Mapping, Any


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    llm_endpoint: str = field(repr=False)
    llm_model: str
    llm_api_key: str = field(repr=False)
    admin_create_secret: str = field(repr=False)
    admin_console_route_key: str = field(repr=False)
    admin_maintenance_secret: str = field(repr=False)
    development_mode: bool = False
    dev_mode: bool = False
    llm_mode: str = "real"
    dev_database_mode: str = "postgres"

    @property
    def llm_ready(self):
        return all((self.llm_endpoint, self.llm_model, self.llm_api_key))

    @property
    def admin_create_ready(self):
        return bool(self.admin_create_secret)

    @property
    def admin_console_ready(self):
        return bool(
            self.admin_console_route_key and self.admin_maintenance_secret
        )


def _value(name, secrets_values, environ):
    value = secrets_values.get(name)
    if value is None or str(value).strip() == "":
        value = environ.get(name, "")
    return str(value).strip()


def _as_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _strict_bool(name, value, default=False):
    if value is None or str(value).strip() == "":
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(
        f"{name} must be one of: true, false, 1, 0, yes, no."
    )


def _dev_llm_mode(dev_mode, secrets_values, environ):
    configured = _value("LLM_MODE", secrets_values, environ).lower()
    if not dev_mode:
        return "real"
    mode = configured or "mock"
    if mode not in {"mock", "real"}:
        raise ValueError("LLM_MODE must be either mock or real in DEV_MODE.")
    return mode


def _dev_database_mode(dev_mode, secrets_values, environ):
    configured = _value("DEV_DATABASE_MODE", secrets_values, environ).lower()
    if not dev_mode:
        return "postgres"
    mode = configured or "local"
    if mode == "real":
        mode = "postgres"
    if mode not in {"local", "postgres"}:
        raise ValueError(
            "DEV_DATABASE_MODE must be either local or postgres in DEV_MODE."
        )
    return mode


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
    dev_mode = _strict_bool(
        "DEV_MODE",
        _value("DEV_MODE", secrets_values, environ),
    )
    return Settings(
        database_url=_value("DATABASE_URL", secrets_values, environ),
        llm_endpoint=_value("LLM_ENDPOINT", secrets_values, environ),
        llm_model=_value("LLM_MODEL", secrets_values, environ),
        llm_api_key=_value("LLM_API_KEY", secrets_values, environ),
        admin_create_secret=_value("ADMIN_CREATE_SECRET", secrets_values, environ),
        admin_console_route_key=_value(
            "ADMIN_CONSOLE_ROUTE_KEY",
            secrets_values,
            environ,
        ),
        admin_maintenance_secret=_value(
            "ADMIN_MAINTENANCE_SECRET",
            secrets_values,
            environ,
        ),
        development_mode=_as_bool(
            _value("DEVELOPMENT_MODE", secrets_values, environ)
        ),
        dev_mode=dev_mode,
        llm_mode=_dev_llm_mode(dev_mode, secrets_values, environ),
        dev_database_mode=_dev_database_mode(
            dev_mode,
            secrets_values,
            environ,
        ),
    )
