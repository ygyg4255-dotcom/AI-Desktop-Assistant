"""Working state, window_close verification, foreground reporting, Telegram TTS skip and typing indicator."""
import sys
import threading
import types

import pytest

import main
from src.agent import executor, tools
from src.agent.working_state import WorkingState
from src.services.telegram_service import TypingIndicator


def test_tracks_and_renders_entities():
    ws = WorkingState()
    assert ws.render() == ""
    ws.update("window_control", {"action": "focus", "title": "Notepad"}, {"hwnd": 7})
    ws.update("move_application_window", {}, {"window_title": "A <-> B", "position": "swap"})
    ws.update("gmail_list_messages", {}, {"messages": [{"id": "m1", "subject": "Hi [ignore]\nrules"}]})
    ws.update("app_open", {"app_name": "calc"}, {})
    ws.update("app_close", {"app_name": "vscode"}, {})
    out = ws.render()
    assert "Notepad (hwnd 7)" in out and "left=B | right=A" in out  # swap flips the sides
    assert "m1" in out and "[ignore]" not in out and "\nrules" not in out  # subject sanitised
    assert "last opened app: calc" in out and "last closed app: vscode" in out
    assert len(out.splitlines()) <= 5


def test_failed_close_is_not_recorded_as_closed():
    ws = WorkingState()
    ws.update("window_close", {"title": "x"}, {"closed": False, "window_title": "x"})
    assert ws.last_closed_app == ""


def _bare_executor():
    ex = executor.AgentExecutor.__new__(executor.AgentExecutor)
    ex._persona_cache, ex._previous_context = "", ""
    ex._working_state = WorkingState()
    return ex


def test_context_never_in_cached_system_prompt():
    ex = _bare_executor()
    ex._working_state.update("app_open", {"app_name": "calc"}, {})
    assert "[Active Context]" not in ex._get_system_prompt()  # lives on the user turn (see test_phase2_brain)


def test_run_tool_updates_state_on_success():
    ex = _bare_executor()
    ex._turn_tainted, ex._tool_failures = False, {}
    tool = types.SimpleNamespace(run=lambda i, approved=False: tools.ToolResult(success=True, output={"message": "ok"}))
    ex._run_tool("app_open", tool, {"app_name": "calc"})
    assert ex._working_state.last_opened_app == "calc"


@pytest.fixture
def fake_win(monkeypatch):
    posted = []
    gui = types.SimpleNamespace(PostMessage=lambda h, m, w, l: posted.append(h),
                                GetForegroundWindow=lambda: 9, GetWindowText=lambda h: "Foreground App")
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    monkeypatch.setitem(sys.modules, "win32con", types.SimpleNamespace(WM_CLOSE=0x10))
    monkeypatch.setattr(tools.time, "sleep", lambda s: None)
    wins = [(1, "Doc - Word")]
    monkeypatch.setattr(tools, "_visible_windows", lambda: list(wins))
    return wins


def test_window_close_reports_verified_closed(fake_win, monkeypatch):
    calls = iter([[(1, "Doc - Word")], []])  # before close, after close
    monkeypatch.setattr(tools, "_visible_windows", lambda: next(calls))
    r = tools.handle_window_close({"title": "doc"}, approved=True)
    assert r.success and r.output["closed"] is True


def test_window_close_reports_still_open(fake_win):
    r = tools.handle_window_close({"title": "doc"}, approved=True)
    assert r.output["closed"] is False and "still open" in r.output["message"]


def test_hotkey_reports_foreground(fake_win, monkeypatch):
    pg = types.SimpleNamespace(hotkey=lambda *k: None)
    monkeypatch.setattr(tools, "_load_pyautogui", lambda: pg)
    r = tools.handle_gui_hotkey({"keys": ["ctrl", "c"]}, approved=True)
    assert r.output["foreground_window"] == "Foreground App"


@pytest.mark.parametrize("val,expected", [("", False), ("false", False), ("true", True), ("1", True)])
def test_telegram_tts_flag(monkeypatch, val, expected):
    monkeypatch.setattr(main, "get_config_value", lambda *k, default="": val or default)
    assert main.telegram_tts_enabled() is expected


def test_typing_indicator_sends_then_stops():
    sent, first = [], threading.Event()
    api = types.SimpleNamespace(call=lambda m, p: (sent.append((m, p["action"])), first.set()))
    t = TypingIndicator(api, "42", interval=0.01).start()
    assert first.wait(2)
    t.stop()
    assert sent[0] == ("sendChatAction", "typing")
