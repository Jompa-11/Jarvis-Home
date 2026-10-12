import logging

from jarvis.logging_utils import RedactingFilter, redact, register_secret
from jarvis.secrets import get_secret


def test_registered_secret_is_redacted():
    register_secret("super-secret-value-123")
    assert "super-secret-value-123" not in redact("token is super-secret-value-123 ok")


def test_pattern_redaction():
    text = (
        "Authorization: Bearer abc.def.ghi key=sk-ant-api03-ABCDEFGHIJKL "
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop password=hunter2"
    )
    out = redact(text)
    for leaked in ("abc.def.ghi", "sk-ant-api03-ABCDEFGHIJKL", "eyJhbGciOiJIUzI1NiJ9", "hunter2"):
        assert leaked not in out


def test_get_secret_registers_value(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "ha-long-lived-token-xyz")
    assert get_secret("HA_TOKEN") == "ha-long-lived-token-xyz"
    assert "ha-long-lived-token-xyz" not in redact("calling with ha-long-lived-token-xyz")


def test_filter_redacts_messages_and_tracebacks(caplog):
    register_secret("trace-secret-999")
    logger = logging.getLogger("jarvis.test")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="jarvis.test"):
        logger.info("value %s", "trace-secret-999")
        try:
            raise RuntimeError("boom trace-secret-999")
        except RuntimeError:
            logger.exception("failed")
    joined = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert "trace-secret-999" not in joined
    assert "[REDACTED]" in joined
