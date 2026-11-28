"""Phase 3: cache-aware sticky tool routing and parallel SAFE tool execution."""
import threading
import time
from unittest.mock import MagicMock

import pytest

from src.agent import executor as executor_mod
from src.agent import tool_router
from src.agent.executor import AgentExecutor
from src.agent.memory import ConversationMemory
from src.agent.tool_router import PACKS
from src.agent.tools import ToolResult, create_default_registry

PACKED = {n for v in PACKS.values() for n in v}


@pytest.fixture
def dynamic(monkeypatch):
    def _set(value):
        monkeypatch.setattr(tool_router, "get_config_value", lambda *a, **k: value)
    _set("true")
    return _set


def _agent(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_mod, "update_hud_status", lambda *a, **k: None)
    ai = MagicMock()
    ai.make_tool_request.return_value = {"content": [{"type": "text", "text": "ok"}]}
    return AgentExecutor(ai_client=ai, database=None,
                         memory=ConversationMemory(memory_file=tmp_path / "memory.json")), ai


def _names(schema):
    return [t["name"] for t in schema]


def test_packs_reference_registered_tools_only_once():
    reg = create_default_registry()
    assert all(reg.get(n) for n in PACKED)
    assert sum(len(v) for v in PACKS.values()) == len(PACKED)


def test_core_present_and_packs_excluded_initially(dynamic):
    names = _names(tool_router.ToolRouter(create_default_registry()).schema())
    assert not PACKED & set(names)
    for core in ("window_control", "process_list", "gui_click", "file_list", "usage_summary", "load_tool_pack"):
        assert core in names


def test_flag_off_sends_full_list(dynamic):
    dynamic("false")
    reg = create_default_registry()
    assert _names(tool_router.ToolRouter(reg).schema()) == [t.name for t in reg.list_tools()]


@pytest.mark.parametrize("msg,pack", [
    ("check my gmail", "email_messaging"), ("افتح البريد", "email_messaging"),
    ("list brave tabs", "browser"), ("اغلق التبويب", "browser"),
    ("launch steam", "gaming_media"), ("open the excel file", "office_docs"),
    ("run this in the terminal", "dev_coding"), ("اكتب كود", "dev_coding"),
])
def test_keyword_preactivation(dynamic, msg, pack):
    r = tool_router.ToolRouter(create_default_registry())
    r.observe(msg)
    assert r.active_packs == [pack]
    assert set(PACKS[pack]) <= set(_names(r.schema()))


def test_sticky_and_deterministic_order(dynamic):
    r = tool_router.ToolRouter(create_default_registry())
    r.observe("open steam")
    r.observe("send an email")  # activation order differs from pack order
    first = _names(r.schema())
    r.observe("what time is it")  # no keywords: nothing flaps
    assert _names(r.schema()) == first
    assert first[-len(PACKS["gaming_media"]):] == list(PACKS["gaming_media"])
    assert r.active_packs == ["email_messaging", "gaming_media"]  # PACKS order, not activation order
    r.reset()
    assert not PACKED & set(_names(r.schema()))


def test_load_tool_pack_activates_and_clear_history_resets(tmp_path, monkeypatch, dynamic):
    ex, _ = _agent(tmp_path, monkeypatch)
    ok = ex._execute_tool("load_tool_pack", {"pack_name": "dev_coding"})
    assert ok.success and "claude_code_run" in _names(ex.tools_schema)
    assert not ex._execute_tool("load_tool_pack", {"pack_name": "nope"}).success
    assert ex._tool_router.active_packs == ["dev_coding"]
    ex.clear_history()
    assert "claude_code_run" not in _names(ex.tools_schema)


def test_payload_tools_grow_across_turns(tmp_path, monkeypatch, dynamic):
    ex, ai = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr(executor_mod, "match_local_intent", lambda *a, **k: None, raising=False)
    sent = []
    ai.make_tool_request.side_effect = lambda payload, **k: (sent.append(_names(payload["tools"])),
                                                             {"content": [{"type": "text", "text": "ok"}]})[1]
    ex._process_sync("tell me a joke")
    ex._process_sync("read my gmail")
    ex._process_sync("tell me another joke")
    assert not PACKED & set(sent[0])
    assert set(PACKS["email_messaging"]) <= set(sent[1])
    assert sent[2] == sent[1]


# --- parallel execution -----------------------------------------------------------

def _uses(*names):
    return [{"id": str(i), "name": n, "input": {}} for i, n in enumerate(names)]


def test_safe_batch_runs_concurrently_and_keeps_order(tmp_path, monkeypatch):
    ex, _ = _agent(tmp_path, monkeypatch)
    barrier = threading.Barrier(3, timeout=3)  # only passes if all 3 run at the same time

    def fake(name, inp, **k):
        barrier.wait()
        time.sleep({"file_list": 0.2, "process_list": 0.0, "system_info": 0.1}[name])
        return ToolResult(success=True, output=name)

    ex._execute_tool_with_permission = fake
    res = ex._run_safe_batch(_uses("file_list", "process_list", "system_info"))
    assert [r.output for r in res] == ["file_list", "process_list", "system_info"]


@pytest.mark.parametrize("names", [
    ("file_list", "terminal_execute"),   # DANGEROUS
    ("file_list", "file_read"),          # CONDITIONAL
    ("file_list", "window_control"),     # SAFE tier but mutates GUI
    ("file_list", "load_tool_pack"),     # mutates router state
    ("file_list",),                      # single call
])
def test_unsafe_batch_falls_back_to_sequential(tmp_path, monkeypatch, names):
    ex, _ = _agent(tmp_path, monkeypatch)
    ex._execute_tool_with_permission = MagicMock()
    assert ex._run_safe_batch(_uses(*names)) is None
    ex._execute_tool_with_permission.assert_not_called()
