"""app_close targets real process names, never reports false success; close fast-path only for known apps."""
from types import SimpleNamespace

import psutil
import pytest

from src.agent.local_router import match_local_intent
from src.agent.tools import handle_app_close


@pytest.fixture(autouse=True)
def _no_grace(monkeypatch):
    monkeypatch.setattr("src.agent.tools._CLOSE_GRACE", 0)
    monkeypatch.setattr("src.agent.tools._post_wm_close", lambda pids: None)


def _proc(name, log, alive=True):
    return SimpleNamespace(pid=1, info={"pid": 1, "name": name}, terminate=lambda: log.append(name),
                           is_running=lambda: alive)


def test_graceful_wm_close_before_force_kill(monkeypatch):
    order, state = [], {"alive": True}
    p = SimpleNamespace(pid=7, info={"pid": 7, "name": "brave.exe"}, is_running=lambda: state["alive"],
                        terminate=lambda: order.append("terminate"))
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [p])
    monkeypatch.setattr("src.agent.tools._post_wm_close", lambda pids: order.append(("wm_close", set(pids))))
    monkeypatch.setattr("src.agent.tools._CLOSE_GRACE", 0.2)
    handle_app_close({"app_name": "brave"})
    assert order == [("wm_close", {7}), "terminate"]  # stubborn process is killed only after WM_CLOSE


def test_no_force_kill_when_process_exits_gracefully(monkeypatch):
    log = []
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [_proc("brave.exe", log, alive=False)])
    r = handle_app_close({"app_name": "brave"})
    assert r.success and r.output["count"] == 1 and log == []


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
    p = SimpleNamespace(pid=1, info={"pid": 1, "name": "Code.exe"}, terminate=deny, is_running=lambda: True)
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [p])
    assert not handle_app_close({"app_name": "vscode"}, approved=True).success


def test_close_known_app_hits_fast_path():
    intent = match_local_intent("close vscode")
    assert intent and intent["tool_name"] == "app_close"
    assert intent["tool_input"] == {"app_name": "vscode"}


@pytest.mark.parametrize("text", ["close terminal claude code", "close all windows vscode"])
def test_contextual_close_falls_through(text):
    assert match_local_intent(text) is None
