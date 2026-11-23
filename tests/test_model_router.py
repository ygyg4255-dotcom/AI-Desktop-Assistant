import io
import json
from unittest.mock import MagicMock

import pytest

from src.services import model_router as mr
from src.services.model_router import ModelRouter

ANTH = {"content": [{"type": "text", "text": "anthropic"}], "stop_reason": "end_turn", "usage": {}}


def _payload(text="how are you today", content=None):
    return {
        "model": "m",
        "system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": content if content is not None else text}],
        "tools": [{"name": "t"}],
    }


@pytest.fixture
def anth():
    a = MagicMock()
    a.make_tool_request.return_value = ANTH
    a.stream_tool_request.return_value = ANTH
    a._model = "claude-x"
    return a


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_open(monkeypatch, lines=None, exc=None, capture=None):
    def opener_open(self, req, timeout=None):
        if capture is not None:
            capture.append(json.loads(req.data))
        if exc:
            raise exc
        return _Resp(("\n".join(json.dumps(x) for x in lines) + "\n").encode())

    monkeypatch.setattr("urllib.request.OpenerDirector.open", opener_open)


GOOD = [{"message": {"content": "hi "}, "done": False}, {"message": {"content": "there"}, "done": True}]


def test_simple_chat_goes_to_ollama_and_strips_cache_control(anth, monkeypatch):
    sent = []
    _fake_open(monkeypatch, GOOD, capture=sent)
    got = []
    r = ModelRouter(anth, enabled=True).stream_tool_request(_payload(), on_text=got.append)
    assert r["content"][0]["text"] == "hi there" and got == ["hi ", "there"]
    anth.stream_tool_request.assert_not_called()
    assert "cache_control" not in json.dumps(sent[0]) and sent[0]["messages"][0] == {"role": "system", "content": "sys"}


def test_non_stream_path_uses_ollama(anth, monkeypatch):
    _fake_open(monkeypatch, GOOD)
    assert ModelRouter(anth, enabled=True).make_tool_request(_payload())["content"][0]["text"] == "hi there"
    anth.make_tool_request.assert_not_called()


@pytest.mark.parametrize("payload", [
    _payload(content=[{"type": "image", "source": {}}, {"type": "text", "text": "what is this"}]),
    _payload("open notepad and then type hello"),
    _payload(content=[{"type": "tool_result", "tool_use_id": "1", "content": "ok"}]),
])
def test_vision_and_tool_turns_go_to_anthropic(anth, monkeypatch, payload):
    _fake_open(monkeypatch, exc=AssertionError("ollama must not be called"))
    assert ModelRouter(anth, enabled=True).make_tool_request(payload) == ANTH


@pytest.mark.parametrize("kw", [
    {"exc": ConnectionRefusedError("down")},
    {"exc": TimeoutError("slow")},
    {"lines": [{"unexpected": 1}]},
    {"lines": [{"message": {"content": ""}, "done": True}]},
])
def test_ollama_failure_falls_back(anth, monkeypatch, kw):
    _fake_open(monkeypatch, **kw)
    assert ModelRouter(anth, enabled=True).stream_tool_request(_payload()) == ANTH


def test_disabled_goes_straight_to_anthropic_with_payload_untouched(anth, monkeypatch):
    _fake_open(monkeypatch, exc=AssertionError("ollama must not be called"))
    p = _payload()
    ModelRouter(anth, enabled=False).make_tool_request(p)
    assert anth.make_tool_request.call_args.args[0] is p  # AIClient still applies caching itself


def test_non_loopback_url_is_refused(anth):
    assert ModelRouter(anth, enabled=True, base_url="http://example.com:11434").enabled is False


def test_attribute_delegation(anth):
    assert ModelRouter(anth, enabled=False)._model == "claude-x"
