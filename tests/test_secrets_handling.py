"""S6: Telegram token redaction and .env handling."""
import io
import urllib.error
from pathlib import Path

import pytest

from src.config import settings
from src.services import telegram_service as tg

TOKEN = "123456789:AAEhBP0av28vWbRYVqzcAbCdEfGhIjKlMnO"


def test_redact_secrets_removes_explicit_and_embedded_tokens():
    msg = f"failed https://api.telegram.org/bot{TOKEN}/sendMessage"
    out = tg.redact_secrets(msg, TOKEN)
    assert TOKEN not in out
    assert "AAEh" not in tg.redact_secrets(msg)  # pattern-based, no explicit token


def test_send_error_print_never_contains_token(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError(f"connect failed for https://api.telegram.org/bot{TOKEN}/sendMessage")
    monkeypatch.setattr(tg.urllib.request, "urlopen", boom)
    assert tg.send_telegram_message("hi", token=TOKEN, chat_id="1") is False
    assert TOKEN not in capsys.readouterr().out


def test_http_error_body_is_redacted(monkeypatch, capsys):
    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(f"bad bot{TOKEN}".encode()))
    monkeypatch.setattr(tg.urllib.request, "urlopen", boom)
    tg.send_telegram_message("hi", token=TOKEN, chat_id="1")
    assert TOKEN not in capsys.readouterr().out


def test_load_env_file_strips_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text('A="hello world"\nB=\'x y\'\nC=plain\nD="unterminated\n', encoding="utf-8")
    v = settings.load_env_file(env)
    assert v["A"] == "hello world" and v["B"] == "x y" and v["C"] == "plain"
    assert v["D"] == '"unterminated'


def test_save_ai_config_roundtrips_values_with_spaces(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setattr(settings.os, "environ", dict(settings.os.environ))
    settings.save_ai_config("claude-sonnet-5-5", "sk-test-1234567", "Brazilian Portuguese", env_path=env)
    v = settings.load_env_file(env)
    assert v["ALTRON_LANGUAGE"] == "Brazilian Portuguese"
    assert v["AI_API_KEY"] == "sk-test-1234567"


def test_save_ai_config_surfaces_write_failure_without_leaking_key(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    monkeypatch.setattr(settings.os, "environ", dict(settings.os.environ))
    real_open = open

    def fail_write(path, mode="r", *a, **k):
        if "w" in mode:
            raise PermissionError("denied")
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr("builtins.open", fail_write)
    with pytest.raises(PermissionError):
        settings.save_ai_config("m", "sk-secret-value-123", "English", env_path=env)
    out = capsys.readouterr().out
    assert "Failed to write" in out and "sk-secret-value-123" not in out
