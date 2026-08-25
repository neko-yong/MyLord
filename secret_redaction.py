import logging
import re


REDACTED_DATABASE_URL = "<REDACTED_DATABASE_URL>"
REDACTED_SECRET = "[REDACTED]"
REDACTED_TOKEN = "<REDACTED_TOKEN>"

_DATABASE_URL_PATTERN = re.compile(
    r"(?i)\b(?:postgresql|postgres)://[^\s\"'<>]+"
)
_DATABASE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(database_url|test_database_url)\s*[:=]\s*[^\s,;\"'}]+"
)
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(llm_api_key|admin_create_secret)\s*[:=]\s*[^\s,;\"'}]+"
)
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,\"'}]+"
)
_API_KEY_PATTERN = re.compile(
    r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,\"'}]+"
)
_RAW_ROLE_TOKEN_PATTERN = re.compile(r"\b[AB]-[A-Za-z0-9_-]{20,}\b")


class PostgresPoolLogFilter(logging.Filter):
    is_postgres_secret_filter = True

    def filter(self, record):
        if record.levelno < logging.WARNING:
            return True
        error_type = next(
            (
                safe_exception_type(argument)
                for argument in record.args
                if isinstance(argument, BaseException)
            ),
            None,
        )
        record.msg = "PostgreSQL pool warning; connection details redacted"
        if error_type:
            record.msg += f" ({error_type})"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


def redact_database_url(value):
    return REDACTED_DATABASE_URL if value else ""


def redact_secrets(value, sensitive_values=()):
    safe = str(value)
    for sensitive_value in sensitive_values:
        if sensitive_value:
            safe = safe.replace(str(sensitive_value), REDACTED_SECRET)
    safe = _DATABASE_URL_PATTERN.sub(REDACTED_DATABASE_URL, safe)
    safe = _DATABASE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}={REDACTED_DATABASE_URL}",
        safe,
    )
    safe = _SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}={REDACTED_SECRET}",
        safe,
    )
    safe = _AUTHORIZATION_PATTERN.sub(rf"\1{REDACTED_SECRET}", safe)
    safe = _API_KEY_PATTERN.sub(rf"\1{REDACTED_SECRET}", safe)
    return _RAW_ROLE_TOKEN_PATTERN.sub(REDACTED_TOKEN, safe)


def safe_exception_type(error):
    return type(error).__name__


def install_postgres_pool_log_redaction():
    logger = logging.getLogger("psycopg.pool")
    if not any(
        getattr(existing, "is_postgres_secret_filter", False)
        for existing in logger.filters
    ):
        logger.addFilter(PostgresPoolLogFilter())
