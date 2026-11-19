"""HUD CONVERSATION feed holds only real chat messages."""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services import hud_status as hs

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def status_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "STATUS_FILE", tmp_path / "status.json")
    hs.init_hud_status()


def test_log_step_goes_to_logger_not_hud(status_file, caplog):
    import main
    with caplog.at_level("INFO"):
        main._log_step(SimpleNamespace(content="Using tool: open_app"))
    assert hs.get_hud_status()["logs"] == []
    assert "Step: Using tool: open_app" in caplog.text


def test_no_step_or_user_said_hud_entries_in_source():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    src += (ROOT / "src" / "agent" / "executor.py").read_text(encoding="utf-8")
    assert not re.search(r"log_entry=f?\"(Step:|User said:)", src)
    assert "Direct listening (Alt+R)\")" not in src.replace("logger", "")
    assert 'log_entry="Direct listening' not in src


def test_user_message_logged_once(status_file):
    hs.update_hud_status(state="Thinking", log_entry="User: what else")
    hs.update_hud_status(state="Thinking")
    assert hs.get_hud_status()["logs"] == ["User: what else"]
