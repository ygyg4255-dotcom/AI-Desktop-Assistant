"""Phase 4: central logging (console + rotating altron.log) with secret redaction."""
import logging
import logging.handlers

import pytest

from src.config import log as altron_log
from src.services import hud_status as hs

BOT_TOKEN = "123456789:AAEhBP0av28vWbRYVqzcAbCdEfGhIjKlMnO"
API_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv"


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path))
    path = altron_log.setup_logging(level="DEBUG", console=False)
    yield path
    altron_log.shutdown_logging()
    logging.getLogger(altron_log.ROOT_LOGGER).setLevel(logging.NOTSET)


def _flush():
    for h in logging.getLogger(altron_log.ROOT_LOGGER).handlers:
        h.flush()


def test_log_file_lives_in_runtime_dir(log_file, tmp_path):
    assert log_file == tmp_path / "altron.log"
    altron_log.get_logger("test").info("hello file")
    _flush()
    assert "hello file" in log_file.read_text(encoding="utf-8")


def test_file_handler_rotates(log_file):
    handlers = logging.getLogger(altron_log.ROOT_LOGGER).handlers
    rotating = [h for h in handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == altron_log.MAX_LOG_BYTES and rotating[0].backupCount >= 1


def test_secrets_redacted_in_messages_and_tracebacks(log_file):
    logger = altron_log.get_logger("test")
    logger.warning("url https://api.telegram.org/bot%s/getUpdates key=%s", BOT_TOKEN, API_KEY)
    try:
        raise RuntimeError(f"failed with {BOT_TOKEN}")
    except RuntimeError:
        logger.exception("boom")
    _flush()
    text = log_file.read_text(encoding="utf-8")
    assert "boom" in text and "RuntimeError" in text
    assert BOT_TOKEN not in text and "AAEh" not in text
    assert API_KEY not in text


def test_setup_is_idempotent(log_file):
    altron_log.setup_logging(level="INFO", console=False)
    altron_log.setup_logging(level="INFO", console=False)
    assert len(logging.getLogger(altron_log.ROOT_LOGGER).handlers) == 1


def test_redact_secrets_api_key_and_bare_token():
    out = altron_log.redact_secrets(f"{API_KEY} {BOT_TOKEN}")
    assert API_KEY not in out and BOT_TOKEN not in out


def test_hud_flush_failure_is_logged_once(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(hs, "STATUS_FILE", tmp_path / "missing-dir" / "x" / "status.json")
    monkeypatch.setattr(hs, "_WARNED", set())

    def broken_write(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(hs.Path, "write_text", broken_write)
    with caplog.at_level(logging.DEBUG, logger="altron"):
        hs.update_hud_status(log_entry="one")
        hs.update_hud_status(log_entry="two")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "status.json" in r.getMessage()]
    assert len(warnings) == 1
