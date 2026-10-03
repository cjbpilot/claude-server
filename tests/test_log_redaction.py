"""The agent log must never contain a Telegram bot token.

httpx logs request URLs at INFO and Telegram embeds the token in the URL
path, so the file handler carries a redaction filter as a backstop.
"""

from __future__ import annotations

import io
import logging

from agent.log_redaction import RedactingFilter, redact_secrets

TOKEN = "123456789:AAH-fake_Token-Value_0123456789abcdef"
URL = f"https://api.telegram.org/bot{TOKEN}/getUpdates"


def _capture() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("test.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, buf


def test_redact_secrets_replaces_token_keeps_path():
    assert redact_secrets(URL) == "https://api.telegram.org/bot<redacted>/getUpdates"


def test_redact_secrets_leaves_other_text_alone():
    s = "connecting to nats://host:4222 as host=chainlocker /bot is fine"
    assert redact_secrets(s) == s


def test_filter_redacts_httpx_style_message_with_args():
    logger, buf = _capture()
    logger.info('HTTP Request: %s %s "%s"', "POST", URL, "HTTP/1.1 200 OK")
    out = buf.getvalue()
    assert TOKEN not in out
    assert "/bot<redacted>/getUpdates" in out
    assert "HTTP/1.1 200 OK" in out


def test_filter_redacts_exception_traceback():
    logger, buf = _capture()
    try:
        raise RuntimeError(f"Client error '404 Not Found' for url '{URL}'")
    except RuntimeError:
        logger.exception("Telegram bot start failed")
    out = buf.getvalue()
    assert TOKEN not in out
    assert "/bot<redacted>/getUpdates" in out
    assert "Traceback" in out
