"""Logging with secret redaction.

Two layers of protection:

* Every secret read through :func:`jarvis.secrets.get_secret` is registered here
  and replaced verbatim in every log record.
* Pattern-based redaction catches common credential shapes (bearer tokens,
  API keys, ``token=`` query parameters) that were never registered.
"""

from __future__ import annotations

import logging
import re
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

_REDACTED = "[REDACTED]"
_secrets: set[str] = set()
_lock = threading.Lock()

_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&]+"),
    # JWT-shaped strings (Home Assistant long-lived tokens are JWTs).
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]


def register_secret(value: str) -> None:
    if value and len(value) >= 6:
        with _lock:
            _secrets.add(value)


def redact(text: str) -> str:
    if not text:
        return text
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        text = text.replace(s, _REDACTED)
    for pat in _PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + _REDACTED, text)
        else:
            text = pat.sub(_REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        record.msg = redact(message)
        record.args = None
        if record.exc_info:
            # Render the traceback now so it is redacted too.
            formatter = logging.Formatter()
            record.exc_text = redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        return True


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        if getattr(h, "_jarvis", False):
            root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    redactor = RedactingFilter()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(redactor)
    console._jarvis = True  # type: ignore[attr-defined]
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "jarvis.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(redactor)
        fh._jarvis = True  # type: ignore[attr-defined]
        root.addHandler(fh)

    # Third-party HTTP libraries can log full URLs and headers at DEBUG.
    for noisy in ("httpx", "httpcore", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLevelName(level)))
