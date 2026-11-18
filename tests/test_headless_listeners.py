"""Phase 4: the headless runtime (main.py) uses Qt-free listeners on plain Python threads."""
import ast
import json
import threading
from pathlib import Path

import pytest

from src.services import telegram_service as tg
from src.services import voice_service as vs
from src.services.signals import CallbackSignal

ROOT = Path(__file__).resolve().parent.parent
CHAT_ID = "4242"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path))


class _Resp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_main_does_not_import_qt():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and m.startswith("PySide6") for m in modules)
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "SpeechRecognitionWorker" not in src and "TelegramRemoteWorker" not in src


def test_listeners_are_not_qt_objects():
    from PySide6.QtCore import QObject
    assert not issubclass(tg.TelegramRemoteListener, QObject)
    assert not issubclass(vs.SpeechRecognitionListener, QObject)
    # Legacy wrappers stay QThreads for the deprecated src/ui/.
    from PySide6.QtCore import QThread
    assert issubclass(tg.TelegramRemoteWorker, QThread)
    assert issubclass(vs.SpeechRecognitionWorker, QThread)


def test_callback_signal_is_per_instance_and_isolates_slot_errors():
    class Thing:
        fired = CallbackSignal()

    a, b, got = Thing(), Thing(), []
    a.fired.connect(lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
    a.fired.connect(got.append)
    a.fired.emit("x")  # first slot raises; second must still run
    b.fired.emit("y")
    assert got == ["x"]


def test_telegram_listener_runs_on_plain_thread(monkeypatch):
    listener = tg.TelegramRemoteListener(bot_token="123:abc", allowed_chat_id=CHAT_ID)
    seen = []
    listener.remote_command_received.connect(
        lambda text: seen.append((text, threading.current_thread().name)))
    polls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        polls["n"] += 1
        if polls["n"] == 1:
            return _Resp({"ok": True, "result": [
                {"update_id": 10, "message": {"chat": {"id": int(CHAT_ID)}, "text": "hello"}},
                {"update_id": 11, "message": {"chat": {"id": 999}, "text": "intruder"}},
            ]})
        listener.stop()
        return _Resp({"ok": True, "result": []})

    monkeypatch.setattr(tg.urllib.request, "urlopen", fake_urlopen)
    t = threading.Thread(target=listener.run, name="TgTest")
    t.start()
    t.join(timeout=10)
    assert not t.is_alive()
    assert seen == [("hello", "TgTest")]
    assert listener._offset == 12


def test_speech_listener_signals_work_without_qt():
    listener = vs.SpeechRecognitionListener(model_size="tiny")
    got = []
    listener.text_recognized.connect(got.append)
    listener.text_recognized.emit("altron open notepad")
    assert got == ["altron open notepad"]
    listener.abort()
    assert listener._is_aborted
