"""Reference-only persistent history, and truthful tool history in the agent loop."""
import json
from unittest.mock import MagicMock

import pytest

from src.agent import executor as executor_mod
from src.agent.approvals import ApprovalManager
from src.agent.executor import AgentExecutor, sanitize_final_reply
from src.agent.history import (
    MAX_STORED_TURNS, REFERENCE_CHARS, REFERENCE_HEADER, REFERENCE_TURNS, PersistentHistory,
)
from src.agent.memory import ConversationMemory
from src.agent.tool_prompts import strip_action_tag, tag_reply, tool_result_prompt
from src.agent.tools import Tool, ToolCategory, ToolResult

ANT_KEY = "sk-ant-api03-" + "A" * 40
GROQ_KEY = "gsk_" + "B1c2D3e4F5g6H7i8J9"
TG_TOKEN = "1234567890:" + "C" * 35


# --- PersistentHistory ----------------------------------------------------------

@pytest.fixture
def hist(tmp_path):
    return PersistentHistory(tmp_path / "conversation_history.json")


def test_secrets_are_redacted_before_they_reach_disk(hist):
    hist.append("user", f"my key is {ANT_KEY} and groq {GROQ_KEY} and bot {TG_TOKEN}")
    raw = hist.path.read_text(encoding="utf-8")
    for secret in (ANT_KEY, GROQ_KEY, TG_TOKEN):
        assert secret not in raw


def test_file_is_capped_to_the_last_turns(hist):
    for i in range(MAX_STORED_TURNS + 15):
        hist.append("user" if i % 2 == 0 else "assistant", f"turn {i}")
    saved = json.loads(hist.path.read_text(encoding="utf-8"))
    assert len(saved) == MAX_STORED_TURNS
    assert saved[-1]["content"] == f"turn {MAX_STORED_TURNS + 14}"
    assert saved[0]["content"] == "turn 15"


def test_very_long_turn_is_truncated_on_disk(hist):
    hist.append("assistant", "x" * 50_000)
    assert len(hist.turns()[0]["content"]) < 2_000


def test_reference_is_compact_labelled_and_truncated(hist):
    for i in range(20):
        hist.append("user", f"question {i} " + "word " * 200)
    ref = hist.load_reference()
    lines = ref.split("\n")
    assert lines[0] == REFERENCE_HEADER
    assert len(lines) == 1 + REFERENCE_TURNS
    assert "question 19" in lines[-1] and "question 12" in lines[1]
    assert all(len(line) <= REFERENCE_CHARS + 20 for line in lines[1:])


def test_empty_history_gives_no_reference(hist):
    assert hist.load_reference() == ""


def test_clear_resets_history(hist):
    hist.append("user", "hello")
    hist.clear()
    assert hist.turns() == [] and hist.load_reference() == ""


def test_corrupt_file_is_ignored_not_fatal(hist):
    hist.path.write_text("{not json", encoding="utf-8")
    assert hist.turns() == []
    hist.append("user", "fresh start")
    assert [t["content"] for t in hist.turns()] == ["fresh start"]


def test_blank_and_bad_roles_are_ignored(hist):
    hist.append("user", "   ")
    hist.append("system", "nope")
    assert hist.turns() == []


# --- executor: previous session is reference-only ---------------------------------

class Probe:
    def __init__(self):
        self.calls = []

    def handler(self, params, approved=False):
        self.calls.append(dict(params))
        return self.result


def make_agent(tmp_path, monkeypatch, history_turns=()):
    monkeypatch.setattr(executor_mod, "update_hud_status", lambda *a, **k: None)
    hist_path = tmp_path / "conversation_history.json"
    if history_turns:
        hist_path.write_text(json.dumps(list(history_turns)), encoding="utf-8")
    ai = MagicMock()
    ex = AgentExecutor(
        ai_client=ai, database=None, approvals=ApprovalManager(dispatch=lambda fn: fn()),
        memory=ConversationMemory(memory_file=tmp_path / "memory.json"),
    )
    ex.ai = ai
    return ex, ai


def register_process_list(ex, result):
    probe = Probe()
    probe.result = result
    ex.registry.register(Tool(
        name="process_list", description="t", category=ToolCategory.UTILITY,
        parameters=[], handler=probe.handler, requires_permission=False,
    ))
    return probe


OLD = [{"role": "user", "content": "I prefer short answers"},
       {"role": "assistant", "content": "Closed WhatsApp for you."}]


def test_previous_session_goes_into_system_prompt_as_reference(tmp_path, monkeypatch):
    ex, _ = make_agent(tmp_path, monkeypatch, OLD)
    prompt = ex._get_system_prompt()
    assert REFERENCE_HEADER in prompt and "I prefer short answers" in prompt
    assert "NEVER assume" in prompt and "PREVIOUS-SESSION MEMORY" in prompt


def test_previous_session_is_not_replayed_as_live_messages(tmp_path, monkeypatch):
    ex, ai = make_agent(tmp_path, monkeypatch, OLD)
    ai.make_tool_request.return_value = {"content": [{"type": "text", "text": "Hi."}]}
    ex._process_sync("hello")
    sent = ai.make_tool_request.call_args[0][0]["messages"]
    assert [m["content"] for m in sent] == ["hello"]       # old turns are not conversation state
    assert ex.memory.get_recent_messages()[0]["content"] == "hello"


def test_no_history_means_no_reference_block(tmp_path, monkeypatch):
    ex, _ = make_agent(tmp_path, monkeypatch)
    assert (REFERENCE_HEADER + chr(10)) not in ex._get_system_prompt()


def test_turns_are_saved_for_the_next_session(tmp_path, monkeypatch):
    ex, ai = make_agent(tmp_path, monkeypatch)
    ai.make_tool_request.return_value = {"content": [{"type": "text", "text": f"Sure {GROQ_KEY}"}]}
    ex._process_sync("remember I like jazz")
    saved = json.loads((tmp_path / "conversation_history.json").read_text(encoding="utf-8"))
    assert [t["role"] for t in saved] == ["user", "assistant"]
    assert GROQ_KEY not in json.dumps(saved)
    # a new executor (restart) sees them only as reference
    ex2, _ = make_agent(tmp_path, monkeypatch)
    assert "remember I like jazz" in ex2._get_system_prompt()


def test_clear_history_forgets_the_previous_session(tmp_path, monkeypatch):
    ex, _ = make_agent(tmp_path, monkeypatch, OLD)
    ex.clear_history()
    assert (REFERENCE_HEADER + chr(10)) not in ex._get_system_prompt()
    assert json.loads((tmp_path / "conversation_history.json").read_text(encoding="utf-8")) == []
    ex2, _ = make_agent(tmp_path, monkeypatch)
    assert (REFERENCE_HEADER + chr(10)) not in ex2._get_system_prompt()


# --- executor: tool history stays truthful ----------------------------------------

def test_system_prompt_forbids_unbacked_action_claims(tmp_path, monkeypatch):
    ex, _ = make_agent(tmp_path, monkeypatch)
    prompt = ex._get_system_prompt()
    assert "TRUTHFULNESS" in prompt and "unless you called a tool in THIS turn" in prompt


def test_real_tool_run_is_tagged_in_history(tmp_path, monkeypatch):
    ex, ai = make_agent(tmp_path, monkeypatch)
    probe = register_process_list(ex, ToolResult(success=True, output={"checked": 50, "total": 254}))
    ai.make_tool_request.side_effect = [
        {"content": [{"type": "tool_use", "id": "t1", "name": "process_list", "input": {}}]},
        {"content": [{"type": "text", "text": "I checked 50 of 254 processes."}]},
    ]
    resp = ex._process_sync("check processes")
    assert probe.calls and resp.tools_used == ["process_list"]
    stored = ex.memory.get_recent_messages()[-1]["content"]
    assert stored.startswith("[Actions run this turn: process_list ok]")
    assert "50 of 254" in stored


def test_turn_without_tools_has_no_action_tag(tmp_path, monkeypatch):
    ex, ai = make_agent(tmp_path, monkeypatch)
    ai.make_tool_request.return_value = {"content": [{"type": "text", "text": "Just chatting."}]}
    ex._process_sync("hi")
    assert ex.memory.get_recent_messages()[-1]["content"] == "Just chatting."


def test_failed_tool_prompt_forbids_claiming_success(tmp_path, monkeypatch):
    ex, ai = make_agent(tmp_path, monkeypatch)
    register_process_list(ex, ToolResult(success=False, output=None, error="access denied"))
    ai.make_tool_request.side_effect = [
        {"content": [{"type": "tool_use", "id": "t1", "name": "process_list", "input": {}}]},
        {"content": [{"type": "text", "text": "That did not work: access denied."}]},
    ]
    ex._process_sync("list processes")
    followup = ai.make_tool_request.call_args_list[1][0][0]["messages"][-1]["content"]
    assert "FAILED" in followup and "Do NOT say or imply the action was completed" in followup
    assert "confirming the action completed" not in followup
    assert ex.memory.get_recent_messages()[-1]["content"].startswith("[Actions run this turn: process_list failed]")


def test_success_prompt_limits_the_reply_to_the_result():
    text = tool_result_prompt("process_list", "{}", True)
    assert "ONLY what the result above shows" in text and "FAILED" not in text


def test_echoed_action_tag_never_reaches_the_user():
    assert sanitize_final_reply("[Actions run this turn: app_close ok] Done.", "x") == "Done."
    assert strip_action_tag("no tag here") == "no tag here"
    assert tag_reply("Done.", ["a ok", "b failed"]) == "[Actions run this turn: a ok, b failed] Done."
    assert tag_reply("Done.", []) == "Done."
