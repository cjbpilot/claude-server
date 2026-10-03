"""Logging filter that keeps secrets (Telegram bot tokens) out of agent.log."""

from __future__ import annotations

import logging
import re

# Telegram puts the bot token in the URL path (https://api.telegram.org/bot<id>:<secret>/getUpdates),
# so any log line or traceback that includes a request URL leaks the token.
_TG_TOKEN_RE = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


def redact_secrets(text: str) -> str:
    return _TG_TOKEN_RE.sub("/bot<redacted>", text)


class RedactingFilter(logging.Filter):
    """Scrub Telegram bot tokens from the rendered message and any traceback.

    Defense in depth: httpx/httpcore are clamped to WARNING so request URLs
    aren't logged at all, but exceptions from python-telegram-bot / httpx can
    still carry the URL in their message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = redact_secrets(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)
        return True
