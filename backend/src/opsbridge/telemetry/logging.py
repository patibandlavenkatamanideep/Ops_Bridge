"""Process logging with redaction of credentials and common identifiers.

Every handler carries `RedactingFilter`: messages, string arguments, exception text and
stack text are scrubbed of bearer tokens, JWTs, provider API keys, secret-like key=value
pairs, sensitive URL query parameters (the OIDC callback `code`/`state` among them) and the
identifier patterns used before hosted model calls. Like that redaction it is
pattern-based and NOT anonymization; code should still avoid logging document content.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

from opsbridge.privacy.redaction import redact

_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:code|state|session_state|token|access_token|id_token|refresh_token|"
    r"client_secret|password|api_key|key|signature|sig|x-amz-signature|x-amz-credential)=)"
    r"[^&\s\"'#]*"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_PROVIDER_KEYS = re.compile(
    r"\b(?:sk-ant-[A-Za-z0-9_-]{10,}|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,})"
)
_SECRET_KV = re.compile(
    r"(?i)\b(password|passwd|secret|client_secret|api[_-]?key|token|authorization|cookie|"
    r"opsbridge_session|x-csrf-token|x-opsbridge-delegation)(\"?\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)


def scrub(text: str) -> str:
    out = _SENSITIVE_QUERY.sub(r"\1[REDACTED]", text)
    out = _BEARER.sub(r"\1 [REDACTED:credential]", out)
    out = _JWT.sub("[REDACTED:jwt]", out)
    out = _PROVIDER_KEYS.sub("[REDACTED:api_key]", out)
    out = _SECRET_KV.sub(r"\1\2[REDACTED]", out)
    return redact(out).text


class RedactingFilter(logging.Filter):
    """Scrubs a record in place before any handler formats it.

    uvicorn's access formatter unpacks `record.args` positionally, so for that logger the
    arguments are scrubbed individually; everywhere else the message is merged and scrubbed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        _correlate(record)
        if record.name == "uvicorn.access" and isinstance(record.args, tuple):
            record.args = tuple(scrub(a) if isinstance(a, str) else a for a in record.args)
        else:
            try:
                message = record.getMessage()
            except Exception:  # a bad format string must not leak or drop the record
                message = f"{record.msg!s} (unformattable arguments withheld)"
            record.msg, record.args = scrub(message), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return True


def _correlate(record: logging.LogRecord) -> None:
    """Attach trace and correlation ids so log lines join to traces (never payloads)."""
    from opentelemetry import trace

    from .context import correlation_id_var

    ctx = trace.get_current_span().get_span_context()
    record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else "-"
    record.correlation_id = correlation_id_var.get() or "-"


_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s [trace=%(trace_id)s cid=%(correlation_id)s] %(message)s"
)


def configure_logging(level: int = logging.INFO) -> None:
    """Root logging for non-uvicorn processes (worker, CLI)."""
    logging.basicConfig(level=level, format=_FORMAT)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())


def uvicorn_log_config(level: str = "info") -> dict[str, Any]:
    """uvicorn's default config plus redaction on every handler and a root logger,
    so application loggers are formatted and filtered too (not uvicorn's last-resort)."""
    from uvicorn.config import LOGGING_CONFIG

    cfg: dict[str, Any] = copy.deepcopy(LOGGING_CONFIG)
    cfg["filters"] = {"redact": {"()": RedactingFilter}}
    for handler in cfg["handlers"].values():
        handler["filters"] = ["redact"]
    cfg["formatters"]["default"]["fmt"] = (
        "%(levelprefix)s [trace=%(trace_id)s cid=%(correlation_id)s] %(message)s"
    )
    cfg["root"] = {"handlers": ["default"], "level": level.upper()}
    for name in ("uvicorn", "uvicorn.access"):
        cfg["loggers"][name]["level"] = level.upper()
    return cfg
