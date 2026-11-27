"""app_close targets real process names, never reports false success; close fast-path only for known apps."""
from types import SimpleNamespace

import psutil
import pytest

from src.agent.local_router import match_local_intent
from src.agent.tools import handle_app_close


def _proc(name, log):
    return SimpleNamespace(info={"pid": 1, "name": name}, terminate=lambda: log.append(name))


def test_vscode_terminates_code_exe(monkeypatch):
    log = []
    procs = [_proc("Code.exe", log), _proc("Code.exe", log), _proc("notepad.exe", log), _proc("Codex.exe", log)]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: procs)
    r = handle_app_close({"app_name": "vscode"}, approved=True)
    assert r.success and r.output["count"] == 2
    assert log == ["Code.exe", "Code.exe"]


def test_no_matching_process_is_failure(monkeypatch):
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [_proc("notepad.exe", [])])
    r = handle_app_close({"app_name": "vscode"}, approved=True)
    assert not r.success and "No running process" in r.error


def test_access_denied_is_failure(monkeypatch):
    def deny():
        raise psutil.AccessDenied()
    p = SimpleNamespace(info={"pid": 1, "name": "Code.exe"}, terminate=deny)
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [p])
    assert not handle_app_close({"app_name": "vscode"}, approved=True).success


def test_close_known_app_hits_fast_path():
    intent = match_local_intent("close vscode")
    assert intent and intent["tool_name"] == "app_close"
    assert intent["tool_input"] == {"app_name": "vscode"}


@pytest.mark.parametrize("text", ["close terminal claude code", "close all windows vscode"])
def test_contextual_close_falls_through(text):
    assert match_local_intent(text) is None
