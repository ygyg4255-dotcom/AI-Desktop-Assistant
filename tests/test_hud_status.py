"""HUD status: in-memory state, atomic flush, cached config."""
import json
import threading

import pytest

from src.services import hud_status as hs
from src.config import settings


@pytest.fixture
def status_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "STATUS_FILE", tmp_path / "status.json")
    hs.init_hud_status()
    return tmp_path / "status.json"


def test_update_is_synchronous_and_dedupes(status_file):
    hs.update_hud_status(state="Thinking", log_entry="a")
    hs.update_hud_status(log_entry="a")
    hs.update_hud_status(log_entry="b")
    data = json.loads(status_file.read_text(encoding="utf-8"))
    assert data["state"] == "Thinking" and data["logs"] == ["a", "b"]
    assert hs.get_hud_status()["logs"] == ["a", "b"]


def test_logs_capped(status_file):
    for i in range(80):
        hs.update_hud_status(log_entry=f"l{i}")
    assert len(hs.get_hud_status()["logs"]) == 50


def test_concurrent_updates_never_corrupt_file(status_file):
    def worker(n):
        for i in range(25):
            hs.update_hud_status(log_entry=f"t{n}-{i}")
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    data = json.loads(status_file.read_text(encoding="utf-8"))  # must parse
    assert isinstance(data["logs"], list) and len(data["logs"]) == 50
    assert not list(status_file.parent.glob("*.tmp"))


def test_get_status_does_not_read_disk(status_file):
    hs.update_hud_status(log_entry="x")
    status_file.write_text("garbage{", encoding="utf-8")
    assert hs.get_hud_status()["logs"] == ["x"]


def test_clear_logs(status_file):
    hs.update_hud_status(log_entry="x")
    hs.clear_hud_logs()
    assert json.loads(status_file.read_text(encoding="utf-8"))["logs"] == []


def test_active_config_cached_until_env_changes(tmp_path, monkeypatch):
    calls = []
    real = settings.Settings

    class Counting(real):
        def __post_init__(self):
            calls.append(1)
            super().__post_init__()
            self.env_path = tmp_path / ".env"
    monkeypatch.setattr(settings, "Settings", Counting)
    settings._CONFIG_CACHE.update(stamp=None, env_path=None, value=None)
    settings.get_active_config_dict()
    settings.get_active_config_dict()
    assert len(calls) == 1
    (tmp_path / ".env").write_text("AI_MODEL=claude-haiku-5-5\n", encoding="utf-8")
    settings.get_active_config_dict()
    assert len(calls) == 2
    settings.invalidate_config_cache()
    settings.get_active_config_dict()
    assert len(calls) == 3
