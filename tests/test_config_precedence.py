"""Phase 4: one canonical config precedence (.env > process env > default)."""
import os

import pytest

from src.config import settings
from src.services import ai_client
from src.services import telegram_service as tg


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setattr(settings, "resolve_env_path", lambda data_dir=None: env)
    # save_ai_config / Settings write os.environ; keep that inside the test.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for key in ("AI_MODEL", "ANTHROPIC_MODEL", "AI_API_KEY", "ANTHROPIC_API_KEY",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ALTRON_LANGUAGE"):
        monkeypatch.delenv(key, raising=False)
    settings.invalidate_config_cache()
    yield env
    settings.invalidate_config_cache()


def test_env_file_beats_process_env(env_file, monkeypatch):
    env_file.write_text("AI_MODEL=claude-from-file\n", encoding="utf-8")
    monkeypatch.setenv("AI_MODEL", "claude-from-env")
    assert settings.get_config_value("AI_MODEL") == "claude-from-file"


def test_process_env_used_when_file_lacks_key(env_file, monkeypatch):
    env_file.write_text("OTHER=1\n", encoding="utf-8")
    monkeypatch.setenv("AI_MODEL", "claude-from-env")
    assert settings.get_config_value("AI_MODEL", default="d") == "claude-from-env"


def test_default_when_unset_and_empty_values_ignored(env_file):
    env_file.write_text("AI_MODEL=\n", encoding="utf-8")
    assert settings.get_config_value("AI_MODEL", default="d") == "d"


def test_aliases_tried_in_order_within_a_source(env_file, monkeypatch):
    env_file.write_text("ANTHROPIC_API_KEY=sk-alias\n", encoding="utf-8")
    monkeypatch.setenv("AI_API_KEY", "sk-env")
    # The file wins as a source even though only the alias is set there.
    assert settings.get_config_value("AI_API_KEY", "ANTHROPIC_API_KEY") == "sk-alias"


def test_env_cache_reloads_when_file_changes(env_file):
    env_file.write_text("AI_MODEL=a\n", encoding="utf-8")
    assert settings.get_config_value("AI_MODEL") == "a"
    env_file.write_text("AI_MODEL=bbbb\n", encoding="utf-8")
    assert settings.get_config_value("AI_MODEL") == "bbbb"


def test_settings_and_telegram_share_the_same_precedence(env_file, monkeypatch):
    env_file.write_text("TELEGRAM_BOT_TOKEN=file-token\nTELEGRAM_CHAT_ID=42\nAI_MODEL=claude-x\n",
                        encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    s = settings.Settings()
    assert s.telegram_bot_token == "file-token"
    assert s.ai_model == "claude-x"
    assert tg.resolve_credentials() == ("file-token", "42")
    # Explicit arguments always win.
    assert tg.resolve_credentials("arg-token", "7") == ("arg-token", "7")


def test_save_ai_config_defaults_to_canonical_env_path(env_file):
    settings.save_ai_config("claude-y", "sk-ant-abc", "English")
    assert "AI_MODEL=claude-y" in env_file.read_text(encoding="utf-8")
    assert settings.get_config_value("AI_MODEL") == "claude-y"


def test_single_default_model_definition():
    assert ai_client.DEFAULT_MODEL is settings.DEFAULT_MODEL
    assert settings.DEFAULT_MODEL == "claude-sonnet-5-5"


def test_agent_tools_use_the_same_telegram_precedence(env_file, monkeypatch):
    from src.agent import tools as tools_mod
    env_file.write_text("TELEGRAM_BOT_TOKEN=file-token\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "env-chat")
    assert tools_mod._telegram_credentials() == ("file-token", "env-chat")
