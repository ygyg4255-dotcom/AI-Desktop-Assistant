"""Local router routes (EN/AR), post-run verifiers, failure counter, PySide6-free services."""
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

import src.agent.executor as executor_mod
from src.agent.executor import AgentExecutor
from src.agent.local_router import match_local_intent
from src.agent.memory import ConversationMemory
from src.agent.tool_prompts import tool_result_prompt
from src.agent.tools import Tool, ToolCategory, ToolResult
from src.agent.verifiers import VERIFIERS, verify_result


@pytest.mark.parametrize("text,tool,args", [
    ("open notepad", "app_open", {"app_name": "notepad"}),
    ("افتح كروم", "app_open", {"app_name": "chrome"}),
    ("close notepad", "app_close", {"app_name": "notepad"}),
    ("اغلق كروم", "app_close", {"app_name": "chrome"}),
    ("find file report in documents", "file_search", {"query": "report", "directory": "~/Documents"}),
    ("ابحث عن ملف تقرير", "file_search", {"query": "تقرير"}),
    ("show my desktop", "file_list", {"directory": "desktop"}),
    ("ماذا في سطح المكتب", "file_list", {"directory": "desktop"}),
    ("pause", "media_control", {"action": "play_pause"}),
    ("next song", "media_control", {"action": "next"}),
    ("التالي", "media_control", {"action": "next"}),
    ("system info", "system_info", {}),
    ("lock my pc", "power_control", {"action": "lock"}),
    ("اقفل الجهاز", "power_control", {"action": "lock"}),
    ("sleep", "power_control", {"action": "sleep"}),
    ("my todos", "todo_list", {}),
    ("ما هي مهامي", "todo_list", {}),
    ("notes", "note_list", {}),
    ("reminders", "list_reminders", {}),
    ("read note shopping", "note_read", {"title_or_id": "shopping"}),
])
def test_table_routes(text, tool, args):
    intent = match_local_intent(text)
    assert intent and intent["tool_name"] == tool and intent["tool_input"] == args


@pytest.mark.parametrize("text", ["what time is it", "كم الساعة؟", "what's the date", "تاريخ اليوم"])
def test_time_and_date_need_no_tool(text):
    intent = match_local_intent(text)
    assert intent["tool_name"] is None and intent["confirmation"]


@pytest.mark.parametrize("text", ["shutdown", "restart", "open the door", "search for cats", "open youtube"])
def test_unrouted_goes_to_llm(text):
    assert match_local_intent(text) is None


@pytest.mark.parametrize("text", ["Close yourself after 1 minute", "remind me in 5 minutes"])
def test_minute_does_not_trigger_mute(text):
    intent = match_local_intent(text)
    assert not intent or intent.get("tool_input") != {"action": "volume_mute"}


@pytest.mark.parametrize("text", ["mute", "mute volume", "unmute"])
def test_mute_still_routes(text):
    assert match_local_intent(text)["tool_input"] == {"action": "volume_mute"}


def _executor(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_mod, "update_hud_status", lambda *a, **k: None)
    return AgentExecutor(ai_client=MagicMock(), database=None,
                         memory=ConversationMemory(memory_file=tmp_path / "memory.json"))


def test_local_time_makes_no_api_call(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    resp = ex._process_sync("what time is it")
    assert resp.success and resp.content and not ex._ai_client.make_tool_request.called
    assert resp.tools_used == []


def test_local_file_list_renders_output(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    ex.registry.register(Tool(name="file_list", description="t", category=ToolCategory.FILE_SYSTEM, parameters=[],
                              handler=lambda p: ToolResult(True, {"entries": [{"name": "a.txt", "type": "file"}]}),
                              requires_permission=False))
    assert "a.txt" in ex._process_sync("show my desktop").content


def test_verifier_flips_missing_file_write(tmp_path):
    res = ToolResult(success=True, output={})
    assert "does not exist" in verify_result("file_write", {"path": str(tmp_path / "nope.txt")}, res)
    ok = tmp_path / "yes.txt"
    ok.write_text("x")
    assert verify_result("file_write", {"path": str(ok)}, res) is None
    assert "still exists" in verify_result("file_delete", {"path": str(ok)}, res)
    assert "file_move" in VERIFIERS


def test_verifier_failure_flips_result_and_counts(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    ex.registry.register(Tool(name="file_write", description="t", category=ToolCategory.FILE_SYSTEM, parameters=[],
                              handler=lambda p: ToolResult(True, {"path": p["path"]}), requires_permission=False))
    res = ex._execute_tool("file_write", {"path": str(tmp_path / "ghost.txt")})
    assert not res.success and "Verification failed" in res.error
    assert ex._tool_failures == {"file_write": 1}


def test_transient_error_retried_once_for_read_only_tool(tmp_path, monkeypatch):
    ex = _executor(tmp_path, monkeypatch)
    monkeypatch.setattr(executor_mod.time, "sleep", lambda s: None)
    calls = []

    def handler(p):
        calls.append(1)
        return ToolResult(len(calls) > 1, {"ok": 1}, error=None if len(calls) > 1 else "operation timed out")
    ex.registry.register(Tool(name="file_list", description="t", category=ToolCategory.FILE_SYSTEM, parameters=[],
                              handler=handler, requires_permission=False))
    assert ex._execute_tool("file_list", {}).success and len(calls) == 2
    calls.clear()
    ex.registry.register(Tool(name="media_control", description="t", category=ToolCategory.SYSTEM, parameters=[],
                              handler=lambda p: (calls.append(1), ToolResult(False, None, error="timed out"))[1],
                              requires_permission=False))
    ex._execute_tool("media_control", {})
    assert len(calls) == 1  # not idempotent: never retried


def test_failure_prompt_allows_one_alternative_then_stops():
    first = tool_result_prompt("file_search", "Error: x", False, failures=1)
    second = tool_result_prompt("file_search", "Error: x", False, failures=2)
    assert "try ONE" in first and "try ONE" not in second


def test_services_never_import_pyside6():
    code = "import sys, src.services, src.services.voice_service, src.services.telegram_service; print('PySide6' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


@pytest.mark.parametrize("text", ["Close settings", "close settings", "hide settings", "اغلق الاعدادات"])
def test_close_settings_does_not_open_settings(text):
    intent = match_local_intent(text) or {}
    assert (intent.get("tool_input") or {}).get("action") != "open_settings"


@pytest.mark.parametrize("text", ["Close New desktop", "close virtual desktop", "close desktop",
                                  "Close window settings", "close tab"])
def test_close_desktop_window_tab_not_app_close(text):
    assert (match_local_intent(text) or {}).get("tool_name") != "app_close"


def test_telegram_polling_backoff_and_deduped_logs(monkeypatch, caplog):
    import logging
    import src.services.telegram_service as ts

    listener = ts.TelegramRemoteListener(bot_token="123:abc", allowed_chat_id="1")
    sleeps, calls = [], {"n": 0}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": true, "result": []}'

    def fake_urlopen(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 5:
            raise OSError("getaddrinfo failed")
        listener.stop()
        return _Resp()

    monkeypatch.setattr(ts.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ts.time, "sleep", sleeps.append)
    monkeypatch.setattr(ts, "save_update_offset", lambda *a: None)
    with caplog.at_level(logging.DEBUG, logger=ts.logger.name):
        listener.run()

    assert sleeps == [2.0, 5.0, 15.0, 30.0, 30.0]
    assert sum("connection lost" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING) == 1
    assert sum("restored" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO) == 1


@pytest.mark.parametrize("p", ["لا ترسل لقطة شاشة", "don't send a screenshot", "no screenshot", "do not take a screenshot please"])
def test_negated_screenshot_does_not_trigger(p):
    r = match_local_intent(p)
    assert not r or r.get("tool_name") != "capture_and_inspect_screen"


def test_screenshot_still_triggers():
    assert match_local_intent("take a screenshot")["tool_name"] == "capture_and_inspect_screen"


@pytest.mark.parametrize("p", ["play", "pause"])
def test_standalone_play_pause_local(p):
    r = match_local_intent(p)
    assert r["tool_name"] == "media_control" and r["tool_input"]["action"] == "play_pause"
