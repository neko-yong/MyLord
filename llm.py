import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse

import requests


MAX_TECHNICAL_DETAIL = 2000
SENSITIVE_QUERY_NAMES = {"api_key", "apikey", "key", "token", "access_token"}
TASK_MAX_TOKENS = {
    "CONNECTION_TEST": 16,
    "DISPUTE_MAP": 3000,
    "INTERVENTION": 1000,
    "JUDGMENT_NORMAL": 6000,
    "JUDGMENT_SWAPPED": 6000,
    "META_JUDGMENT": 6000,
}


@dataclass(frozen=True)
class LLMResult:
    content: str
    model: str
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    latency_ms: float


class LLMError(Exception):
    def __init__(
        self,
        category,
        technical_detail="",
        status_code=None,
        *,
        model=None,
        finish_reason=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        latency_ms=None,
    ):
        super().__init__("AI 法官暂时无法响应，请稍后重试。")
        self.category = category
        self.status_code = status_code
        self.technical_detail = technical_detail[:MAX_TECHNICAL_DETAIL]
        self.model = model
        self.finish_reason = finish_reason
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.latency_ms = latency_ms

    def debug_summary(self):
        lines = [f"category: {self.category}"]
        if self.status_code is not None:
            lines.append(f"http_status: {self.status_code}")
        if self.model is not None:
            lines.append(f"model: {self.model}")
        if self.finish_reason is not None:
            lines.append(f"finish_reason: {self.finish_reason}")
        if self.prompt_tokens is not None:
            lines.append(f"prompt_tokens: {self.prompt_tokens}")
        if self.completion_tokens is not None:
            lines.append(f"completion_tokens: {self.completion_tokens}")
        if self.total_tokens is not None:
            lines.append(f"total_tokens: {self.total_tokens}")
        if self.latency_ms is not None:
            lines.append(f"latency_ms: {self.latency_ms:.2f}")
        if self.technical_detail:
            lines.append(self.technical_detail)
        return "\n".join(lines)


def _redact(text, api_key):
    safe = str(text)
    if api_key:
        safe = safe.replace(api_key, "[REDACTED]")
    safe = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,\"'}]+",
        r"\1[REDACTED]",
        safe,
    )
    safe = re.sub(
        r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,\"'}]+",
        r"\1[REDACTED]",
        safe,
    )
    return safe[:MAX_TECHNICAL_DETAIL]


def _validate_configuration(endpoint, model, api_key):
    if not endpoint or not model or not api_key:
        raise LLMError("configuration", "LLM server secrets are incomplete.")

    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LLMError("configuration", "LLM_ENDPOINT is not a valid HTTP URL.")
    if parsed.username or parsed.password:
        raise LLMError("configuration", "LLM_ENDPOINT must not contain credentials.")

    query_names = {name.lower() for name, _ in parse_qsl(parsed.query)}
    if query_names & SENSITIVE_QUERY_NAMES:
        raise LLMError("configuration", "LLM credentials must not be placed in the URL.")


def _http_error(response, api_key):
    status = response.status_code
    detail = _redact(response.text, api_key)
    if status in {401, 403}:
        category = "authentication"
    elif status == 404:
        category = "not_found"
    elif status == 429:
        category = "rate_limit"
    elif 500 <= status <= 599:
        category = "provider_server"
    elif status == 400:
        category = "request_or_model"
    else:
        category = "http"
    return LLMError(category, detail, status_code=status)


def _is_deepseek_endpoint(endpoint):
    hostname = (urlparse(endpoint).hostname or "").lower()
    return hostname == "deepseek.com" or hostname.endswith(".deepseek.com")


def _optional_token_count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def call_llm(
    endpoint,
    model,
    api_key,
    system_prompt,
    user_prompt,
    temperature=0.2,
    timeout=120,
    max_tokens=None,
):
    endpoint = endpoint.strip()
    model = model.strip()
    api_key = api_key.strip()
    _validate_configuration(endpoint, model, api_key)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
            raise LLMError("configuration", "max_tokens must be a positive integer.")
        payload["max_tokens"] = max_tokens
    if _is_deepseek_endpoint(endpoint):
        payload["thinking"] = {"type": "disabled"}

    started = time.perf_counter()
    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=timeout,
        )
    except requests.Timeout as exc:
        raise LLMError("timeout", "The LLM request timed out.") from exc
    except requests.ConnectionError as exc:
        raise LLMError("connection", "The LLM service could not be reached.") from exc
    except requests.RequestException as exc:
        raise LLMError("request", _redact(exc, api_key)) from exc

    latency_ms = (time.perf_counter() - started) * 1000

    if not response.ok:
        raise _http_error(response, api_key)

    try:
        data = response.json()
    except ValueError as exc:
        raise LLMError(
            "response_json",
            _redact(response.text, api_key),
            status_code=response.status_code,
        ) from exc

    try:
        choices = data["choices"]
        choice = choices[0]
        message = choice["message"]
        content = message["content"]
        if not isinstance(content, str) or not content.strip():
            raise TypeError("message.content is empty or not text")
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(
            "response_schema",
            "Chat completion response did not contain usable final content.",
            status_code=response.status_code,
        ) from exc

    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        finish_reason = None

    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = _optional_token_count(usage.get("prompt_tokens"))
    completion_tokens = _optional_token_count(usage.get("completion_tokens"))
    total_tokens = _optional_token_count(usage.get("total_tokens"))
    response_model = data.get("model")
    if not isinstance(response_model, str) or not response_model.strip():
        response_model = model

    if finish_reason == "length":
        raise LLMError(
            "truncated",
            "The LLM response reached the configured token limit.",
            status_code=response.status_code,
            model=response_model,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
        )

    return LLMResult(
        content=content,
        model=response_model,
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
    )
