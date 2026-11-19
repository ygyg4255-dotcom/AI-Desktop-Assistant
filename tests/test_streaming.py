"""Claude streaming (SSE) + sentence chunking, with the S3/S4 gate left intact."""
import json
from unittest.mock import MagicMock

import pytest

from src.agent import executor as executor_mod
from src.agent.approvals import ApprovalManager
from src.agent.executor import AgentExecutor, SpeechStreamer
from src.agent.memory import ConversationMemory
from src.agent.tools import Tool, ToolCategory, ToolParameter, ToolResult
from src.services.ai_client import AIClient, parse_sse_stream
from src.services.sentence_chunker import SentenceChunker


# --- sentence chunking -------------------------------------------------------

def chunk(deltas):
    c, out = SentenceChunker(), []
    for d in deltas:
        out += c.feed(d)
    return out, c.flush()


def test_splits_on_all_terminators_and_newline():
    done, rest = chunk(["One. Two! Three? Four؟ Five، Six\nSeven"])
    assert done == ["One.", "Two!", "Three?", "Four؟", "Five،", "Six"]
    assert rest == ["Seven"]


def test_sentence_split_across_deltas_emits_as_soon_as_complete():
    c = SentenceChunker()
    assert c.feed("Hello wor") == []
    assert c.feed("ld. How") == ["Hello world."]
    assert c.feed(" are you?") == ["How are you?"]  # '?' at buffer end is final
    assert c.flush() == []


def test_decimal_point_split_across_deltas_is_not_a_boundary():
    done, rest = chunk(["Pi is 3.", "14 roughly. Done"])
    assert done == ["Pi is 3.14 roughly."]
    assert rest == ["Done"]


def test_trailing_period_waits_until_flush():
    c = SentenceChunker()
    assert c.feed("Finished.") == []
    assert c.flush() == ["Finished."]


def test_ellipsis_and_punctuation_only_fragments():
    done, rest = chunk(["Wait... ok?! \n\n Next"])
    assert done == ["Wait...", "ok?!"]
    assert rest == ["Next"]


def test_arabic_text_chunks():
    done, rest = chunk(["مرحبا بك، كيف حالك؟ أنا بخير."])
    assert done == ["مرحبا بك،", "كيف حالك؟"]
    assert rest == ["أنا بخير."]


def test_code_fences_are_never_emitted():
    done, rest = chunk(["Here you go. ```python\nx = 1.\nprint(x)\n```\nThat is all."])
    assert "Here you go." in done and "That is all." in done + rest
    assert not any("x = 1" in s or "print" in s for s in done + rest)


# --- SSE parsing -------------------------------------------------------------

def sse(*events):
    lines = []
    for ev in events:
        lines += [f"event: {ev['type']}", "data: " + json.dumps(ev), ""]
    return [(line + "\n").encode() for line in lines]


TEXT_STREAM = sse(
    {"type": "message_start", "message": {"id": "m1", "model": "claude-x", "role": "assistant", "usage": {"input_tokens": 5}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello. "}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "World!"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
    {"type": "message_stop"},
)

TOOL_STREAM = sse(
    {"type": "message_start", "message": {"id": "m2", "model": "claude-x", "role": "assistant", "usage": {}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "On it."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1,
     "content_block": {"type": "tool_use", "id": "tu1", "name": "fake_danger", "input": {}}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"path\": "}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "\"C:/a\"}"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}},
)


def test_parse_sse_text_and_callback():
    seen = []
    msg = parse_sse_stream(TEXT_STREAM, seen.append)
    assert seen == ["Hello. ", "World!"]
    assert msg["content"] == [{"type": "text", "text": "Hello. World!"}]
    assert msg["stop_reason"] == "end_turn"
    assert msg["usage"] == {"input_tokens": 5, "output_tokens": 4}


def test_parse_sse_assembles_tool_use_input():
    msg = parse_sse_stream(TOOL_STREAM)
    tool = msg["content"][1]
    assert tool["type"] == "tool_use" and tool["name"] == "fake_danger"
    assert tool["input"] == {"path": "C:/a"}
    assert msg["stop_reason"] == "tool_use"


def test_parse_sse_error_event_raises():
    with pytest.raises(ConnectionError):
        parse_sse_stream(sse({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}))


def test_stream_request_sets_stream_flag_and_hardcoded_url(monkeypatch):
    from src.services import ai_client as mod
    seen = {}

    class Resp:
        def __enter__(self):
            return iter(TEXT_STREAM)

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data)
        return Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    out = []
    msg = AIClient(api_key="k").stream_tool_request({"messages": []}, on_text=out.append)
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["body"]["stream"] is True
    assert "".join(out) == "Hello. World!" and msg["content"][0]["text"] == "Hello. World!"


# --- executor: sentences reach the TTS callback; the gate is unchanged ---------

class Danger:
    def __init__(self):
        self.runs = []

    def handler(self, params, approved=False):
        if not approved:
            return ToolResult(success=False, output=None, requires_permission=True,
                              permission_message="Danger")
        self.runs.append(dict(params))
        return ToolResult(success=True, output={"message": "done"})


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.setattr(executor_mod, "update_hud_status", lambda *a, **k: None)
    ai = MagicMock()
    ex = AgentExecutor(ai_client=ai, database=None, approvals=ApprovalManager(dispatch=lambda fn: fn()),
                       memory=ConversationMemory(memory_file=tmp_path / "memory.json"))
    ex.danger = Danger()
    ex.registry.register(Tool(
        name="fake_danger", description="t", category=ToolCategory.UTILITY,
        parameters=[ToolParameter(name="path", type="string", description="p")],
        handler=ex.danger.handler, requires_permission=True,
    ))
    ex.ai = ai
    return ex


def _stream_with(deltas, final):
    def fake(payload, on_text=None, **kw):
        for d in deltas:
            on_text(d)
        return final
    return fake


def test_text_turn_dispatches_sentences_and_marks_streamed(agent):
    final = {"content": [{"type": "text", "text": "First one. Second one!"}]}
    agent.ai.stream_tool_request.side_effect = _stream_with(["First one. Sec", "ond one!"], final)
    spoken = []
    resp = agent._process_sync("hi", on_sentence=spoken.append)
    assert spoken == ["First one.", "Second one!"]
    assert resp.streamed_speech is True and "First one." in resp.content
    agent.ai.make_tool_request.assert_not_called()


def test_without_callback_uses_non_streaming_path(agent):
    agent.ai.make_tool_request.return_value = {"content": [{"type": "text", "text": "Plain."}]}
    resp = agent._process_sync("hi")
    assert resp.streamed_speech is False
    agent.ai.stream_tool_request.assert_not_called()


def test_streamed_tool_use_still_goes_through_approval_gate(agent):
    final = {"content": [
        {"type": "text", "text": "On it."},
        {"type": "tool_use", "id": "t1", "name": "fake_danger", "input": {"path": "C:/a", "_approved": True}},
    ]}
    agent.ai.stream_tool_request.side_effect = _stream_with(["On it."], final)
    spoken = []
    resp = agent._process_sync("do it", origin="voice", on_sentence=spoken.append)
    assert agent.danger.runs == []                       # S3/S4: not executed
    assert len(agent.approvals.list_pending()) == 1      # approval requested instead
    assert resp.streamed_speech is False                 # approval notice is spoken by the caller
    assert spoken == ["On it."]


def test_leaked_tool_call_text_is_not_spoken():
    spoken = []
    s = SpeechStreamer(spoken.append)
    s.feed("Sure thing. Using tool: terminal_execute with input: {\"cmd\": \"x.\"} Done.")
    s.flush()
    assert spoken == ["Sure thing."]


def test_callback_errors_do_not_break_the_turn():
    def boom(_):
        raise RuntimeError("tts down")

    s = SpeechStreamer(boom)
    s.feed("Hello there. ")
    s.flush()
    assert s.spoke is False
