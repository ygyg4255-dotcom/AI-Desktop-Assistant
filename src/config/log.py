"""Central logging for Altron: console + rotating file, with secret redaction.

Usage::

    from src.config.log import get_logger
    logger = get_logger(__name__)          # -> "altron.<module>"

The runtime entry points (main.py, ``python ui_server.py``) call
``setup_logging()`` once. Every handler it installs formats through
``RedactingFormatter``, so Telegram bot tokens, Anthropic and Groq API keys are
stripped from messages *and* tracebacks before they reach the console or
``~/.ai-desktop-assistant/altron.log`` (audit S6/S8). Without
``setup_logging()`` (tests, library use) records propagate to the root logger
as usual, so pytest's ``caplog`` sees them.
"""
import logging
import logging.handlers
import os
import re
import sys
import threading
from pathlib import Path
from typing import Optional

from src.config.paths import runtime_file

ROOT_LOGGER = "altron"
LOG_FILE_NAME = "altron.log"
LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
MAX_LOG_BYTES = 1_000_000
LOG_BACKUPS = 3

_BOT_TOKEN_RE = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")
_BARE_BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}")
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")
_GROQ_KEY_RE = re.compile(r"gsk_[A-Za-z0-9_-]{8,}")

# Google OAuth: access token (ya29.), refresh token (1//), client secret (GOCSPX-)
_GOOGLE_OAUTH_RE = re.compile(r"ya29\.[A-Za-z0-9_.-]{20,}|\b1//[A-Za-z0-9_-]{20,}|GOCSPX-[A-Za-z0-9_-]{10,}")

_SETUP_LOCK =threading.Lock()
_INSTALLED: list = []  # (logger, handler) pairs added by setup_logging


def redact_secrets(text: object, *tokens: Optional[str]) -> str:
    """Strip explicit secrets plus anything shaped like a bot token or API key (audit S6)."""
    out = str(text)
    for token in tokens:
        if token:
            out = out.replace(token, "<redacted>")
    out = _BOT_TOKEN_RE.sub("bot<redacted>", out)
    out = _BARE_BOT_TOKEN_RE.sub("<redacted>", out)
    out = _ANTHROPIC_KEY_RE.sub("sk-ant-<redacted>", out)
    out = _GROQ_KEY_RE.sub("gsk_<redacted>", out)
    return _GOOGLE_OAUTH_RE.sub("<redacted-google-oauth>", out)


class RedactingFormatter(logging.Formatter):
    """Formatter whose whole output (message + traceback) passes through redact_secrets."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def get_logger(name: str) -> logging.Logger:
    """Logger under the "altron" namespace ("src.services.x" -> "altron.src.services.x")."""
    if name == ROOT_LOGGER or name.startswith(ROOT_LOGGER + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")


def default_log_path() -> Path:
    return runtime_file(LOG_FILE_NAME)


def setup_logging(level: Optional[str] = None, log_file: Optional[Path] = None,
                  console: bool = True) -> Path:
    """Install console + rotating-file handlers on the "altron" logger (idempotent).

    ``level`` defaults to $ALTRON_LOG_LEVEL or INFO. Returns the log file path.
    """
    level_name = (level or os.environ.get("ALTRON_LOG_LEVEL") or "INFO").upper()
    path = Path(log_file) if log_file else default_log_path()
    formatter = RedactingFormatter(LOG_FORMAT)

    with _SETUP_LOCK:
        _remove_installed_handlers()
        handlers = []
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8", delay=True)
            handlers.append(fh)
        except OSError as err:
            sys.stderr.write(f"[Altron] File logging disabled ({type(err).__name__})\n")
        if console:
            stream = sys.stderr
            try:
                stream.reconfigure(errors="backslashreplace")  # emoji on legacy consoles
            except (AttributeError, ValueError):
                pass
            handlers.append(logging.StreamHandler(stream))

        root = logging.getLogger(ROOT_LOGGER)
        root.setLevel(getattr(logging, level_name, logging.INFO))
        # Werkzeug logs every HUD poll at INFO; keep only its errors, in the same sinks.
        werkzeug = logging.getLogger("werkzeug")
        werkzeug.setLevel(logging.ERROR)
        for handler in handlers:
            handler.setFormatter(formatter)
            for target in (root, werkzeug):
                target.addHandler(handler)
                _INSTALLED.append((target, handler))
    return path


def shutdown_logging() -> None:
    """Remove and close the handlers installed by setup_logging."""
    with _SETUP_LOCK:
        _remove_installed_handlers()


def _remove_installed_handlers() -> None:
    closed = set()
    for target, handler in _INSTALLED:
        target.removeHandler(handler)
        if id(handler) not in closed:
            closed.add(id(handler))
            try:
                handler.close()
            except Exception:
                pass
    _INSTALLED.clear()
