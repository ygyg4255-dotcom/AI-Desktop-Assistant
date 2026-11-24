"""Multi-provider config, OpenAI-compatible translation, loopback guard, Anthropic caching intact."""
import io
import json
from unittest.mock import MagicMock

import pytest

import ui_server
from src.config import settings as S
from src.services import openai_compat as oc
from src.services.model_router import ModelRouter

PORT = 5000
BASE = f"http://127.0.0.1:{PORT}"
ENV_KEYS = [n for e in S.PROVIDER_ENV.values() for n in (*e["key"], *e["model"], e["url"] or "AI_PROVIDER")] + ["AI_PROVIDER"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    monkeypatch.setattr(S, "resolve_env_path", lambda *a, **k: f)
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    S.invalidate_config_cache()
    yield f
    for k in ENV_KEYS:  # save_ai_config writes os.environ directly
        monkeypatch.delenv(k, raising=False)
    S.invalidate_config_cache()


# ---- persistence / key isolation ------------------------------------------

def test_keys_models_urls_are_isolated_per_provider(env):
    S.save_ai_config("claude-x", "sk-ant-AAAA1111", "English", provider="anthropic")
    S.save_ai_config("gpt-4o", "sk-openai-BBBB2222", "English", provider="openai")
    S.save_ai_config("llama3", "", "English", provider="ollama", base_url="http://localhost:11434")
    S.save_ai_config("m1", "sk-custom-CCCC3333", "English", provider="custom", base_url="https://api.groq.com/openai/v1")
    S.save_ai_config("gemini-2.5-pro", "AIza-DDDD4444", "English", provider="gemini")
    S.save_ai_config("gpt-4o-mini", "", "English", provider="openai")  # blank key keeps the old one
    text = env.read_text(encoding="utf-8")
    assert "AI_PROVIDER=openai" in text
    for secret in ("sk-ant-AAAA1111", "sk-openai-BBBB2222", "sk-custom-CCCC3333", "AIza-DDDD4444"):
        assert secret in text
    cfg = S.Settings().get_config_dict()
    p = cfg["providers"]
    assert cfg["provider"] == "openai" and cfg["model"] == "gpt-4o-mini" and cfg["has_api_key"]
    assert p["anthropic"]["model"] == "claude-x" and p["gemini"]["model"] == "gemini-2.5-pro"
    assert p["ollama"]["base_url"] == "http://localhost:11434" and not p["ollama"]["has_api_key"]
    assert p["custom"]["base_url"] == "https://api.groq.com/openai/v1"
    assert all(p[x]["has_api_key"] for x in ("anthropic", "openai", "gemini", "custom"))
    assert "sk-openai-BBBB2222" not in json.dumps(cfg)  # masked


def test_legacy_save_keeps_provider_and_edits_anthropic(env):
    S.save_ai_config("m", "sk-o", provider="openai")
    S.save_ai_config("claude-y", "sk-ant-1234567890", "English")
    assert S.get_active_provider() == "openai"
    assert S.get_provider_config("anthropic")["model"] == "claude-y"


def test_unknown_provider_and_newline_rejected(env):
    with pytest.raises(ValueError):
        S.save_ai_config("m", "", provider="evil")
    with pytest.raises(ValueError):
        S.save_ai_config("m", "", provider="custom", base_url="https://x\nAI_API_KEY=1")


# ---- HUD endpoints ----------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ui_server, "_HUD_PORT", PORT)
    saved = MagicMock()
    monkeypatch.setattr(ui_server, "save_ai_config", saved)
    c = ui_server.flask_app.test_client()
    c.saved = saved
    return c


def _post(client, path, **body):
    return client.post(path, base_url=BASE, headers={"X-HUD-Token": ui_server._HUD_TOKEN}, json=body)


def test_config_post_passes_provider_and_base_url(client):
    r = _post(client, "/config", provider="custom", model="m", base_url="https://openrouter.ai/api/v1", api_key="k")
    assert r.status_code == 200
    kw = client.saved.call_args.kwargs
    assert (kw["provider"], kw["base_url"]) == ("custom", "https://openrouter.ai/api/v1")


@pytest.mark.parametrize("provider,url", [
    ("ollama", "http://evil.example:11434"), ("ollama", "http://127.0.0.1.evil.com"), ("ollama", "file:///etc/passwd"),
    ("custom", "http://evil.example/v1"), ("custom", "ftp://x.com"), ("custom", ""),
])
@pytest.mark.parametrize("path", ["/config", "/test_config"])
def test_bad_base_urls_rejected(client, path, provider, url):
    r = _post(client, path, provider=provider, model="m", base_url=url)
    assert r.status_code == 400
    client.saved.assert_not_called()


def test_loopback_http_allowed_for_custom_and_ollama(client):
    assert _post(client, "/config", provider="custom", model="m", base_url="http://127.0.0.1:8080/v1").status_code == 200
    assert _post(client, "/config", provider="ollama", model="m", base_url="http://localhost:11434").status_code == 200


def test_test_config_uses_selected_providers_stored_key(client, env, monkeypatch):
    S.save_ai_config("gpt-4o-mini", "sk-openai-REAL", provider="openai")
    made = {}

    class Fake(oc.OpenAICompatClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.update(provider=self.provider, key=self._api_key, model=self._model)

        def test_connection(self):
            return True, "ok"

    monkeypatch.setattr(ui_server, "OpenAICompatClient", Fake)
    r = _post(client, "/test_config", provider="openai", model="", api_key="")
    assert r.get_json()["ok"] and made == {"provider": "openai", "key": "sk-openai-REAL", "model": "gpt-4o-mini"}


# ---- translation ------------------------------------------------------------

PAYLOAD = {
    "model": "claude", "max_tokens": 100,
    "system": [{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}],
    "tools": [{"name": "open_app", "description": "d", "input_schema": {"type": "object", "properties": {"n": {"type": "string"}}}}],
    "messages": [
        {"role": "user", "content": "open notepad"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                          {"type": "tool_use", "id": "t1", "name": "open_app", "input": {"n": "notepad"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "opened"},
                                     {"type": "text", "text": "thanks"}]},
    ],
}


def test_request_translation():
    b = oc.to_openai_request(PAYLOAD, "gpt-4o-mini", stream=True)
    assert b["model"] == "gpt-4o-mini" and b["stream"] and b["max_tokens"] == 100
    assert "cache_control" not in json.dumps(b)
    assert b["tools"][0] == {"type": "function", "function": {"name": "open_app", "description": "d",
                                                              "parameters": PAYLOAD["tools"][0]["input_schema"]}}
    roles = [m["role"] for m in b["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert b["messages"][2]["tool_calls"][0]["function"] == {"name": "open_app", "arguments": '{"n": "notepad"}'}
    assert b["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": "opened"}


def test_response_translation_with_tool_call():
    r = oc.from_openai_response({"choices": [{"finish_reason": "tool_calls", "message": {
        "content": "sure", "tool_calls": [{"id": "c1", "function": {"name": "open_app", "arguments": '{"n": "x"}'}}]}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4}}, "m")
    assert r["stop_reason"] == "tool_use" and r["usage"] == {"input_tokens": 3, "output_tokens": 4}
    assert r["content"] == [{"type": "text", "text": "sure"},
                            {"type": "tool_use", "id": "c1", "name": "open_app", "input": {"n": "x"}}]


def test_stream_assembles_text_and_split_tool_call():
    lines = [f"data: {json.dumps(c)}" for c in (
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "open_app", "arguments": '{"n"'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ': "x"}'}}]}, "finish_reason": "tool_calls"}]},
    )] + ["data: [DONE]"]
    got = []
    r = oc.parse_openai_stream(lines, "m", got.append)
    assert got == ["Hel", "lo"] and r["stop_reason"] == "tool_use"
    assert r["content"][1] == {"type": "tool_use", "id": "c1", "name": "open_app", "input": {"n": "x"}}


def test_stream_parallel_indexless_tool_calls():
    tc = lambda i, n, a: {"choices": [{"delta": {"tool_calls": [{"id": i, "function": {"name": n, "arguments": a}}]}}]}
    lines = [f"data: {json.dumps(c)}" for c in (tc("a", "open_app", "{}"), tc("b", "file_list", "{}"))]
    r = oc.parse_openai_stream(lines, "m")
    assert [b["name"] for b in r["content"]] == ["open_app", "file_list"]


def test_custom_provider_requires_model(client):
    r = _post(client, "/config", provider="custom", model=" ", base_url="https://x.example/v1")
    assert r.status_code == 400


def test_redirect_is_blocked_not_followed(monkeypatch):
    import http.client
    import urllib.request

    class Conn:  # fake HTTP connection answering 302 to another host
        def __init__(self, *a, **k): pass
        sock = None
        def set_debuglevel(self, *a): pass
        def request(self, *a, **k): pass
        def getresponse(self):
            r = http.client.HTTPResponse(MagicMock())
            r.fp = io.BytesIO(b"HTTP/1.1 302 Found\r\nLocation: https://evil.example/\r\n\r\n")
            r.begin()
            return r
        def close(self): pass
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", lambda self, req: self.do_open(Conn, req))
    with pytest.raises(ConnectionError) as e:
        oc.OpenAICompatClient("openai", "", "sk-k", "m").make_tool_request(PAYLOAD, max_attempts=1)
    assert "302" in str(e.value)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("provider,base,expect_url,key", [
    ("openai", "", "https://api.openai.com/v1/chat/completions", "sk-o"),
    ("gemini", "", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "AIza"),
    ("custom", "https://api.groq.com/openai/v1/", "https://api.groq.com/openai/v1/chat/completions", "gk"),
    ("ollama", "http://127.0.0.1:11434", "http://127.0.0.1:11434/v1/chat/completions", ""),
])
def test_client_hits_right_endpoint_with_bearer(monkeypatch, provider, base, expect_url, key):
    seen = {}

    def fake_open(self, req, timeout=None):
        seen.update(url=req.full_url, auth=req.get_header("Authorization"), body=json.loads(req.data))
        return _Resp(json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode())

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)
    r = oc.OpenAICompatClient(provider, base, key, "mdl").make_tool_request(PAYLOAD)
    assert r["content"][0]["text"] == "hi" and seen["url"] == expect_url
    assert seen["auth"] == (f"Bearer {key}" if key else None) and seen["body"]["model"] == "mdl"


@pytest.mark.parametrize("provider,url", [("ollama", "http://evil.example:11434"), ("custom", "http://evil.example/v1")])
def test_client_refuses_bad_urls_without_network(monkeypatch, provider, url):
    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda *a, **k: pytest.fail("network call"))
    with pytest.raises(ValueError):
        oc.OpenAICompatClient(provider, url, "k", "m").make_tool_request(PAYLOAD)


def test_http_error_redacts_key(monkeypatch):
    import urllib.error

    def boom(self, req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(b'bad key sk-secret-1'))

    monkeypatch.setattr("urllib.request.OpenerDirector.open", boom)
    with pytest.raises(ConnectionError) as e:
        oc.OpenAICompatClient("openai", "", "sk-secret-1", "m").make_tool_request(PAYLOAD, max_attempts=1)
    assert "sk-secret-1" not in str(e.value) and "401" in str(e.value)


# ---- router -----------------------------------------------------------------

ANTH = {"content": [{"type": "text", "text": "a"}], "stop_reason": "end_turn", "usage": {}}


def test_router_defaults_to_anthropic_with_payload_untouched(env):
    a = MagicMock()
    a.make_tool_request.return_value = ANTH
    a.stream_tool_request.return_value = ANTH
    r = ModelRouter(a)
    assert r.make_tool_request(PAYLOAD) == ANTH and a.make_tool_request.call_args.args[0] is PAYLOAD
    r.stream_tool_request(PAYLOAD, on_text=print)
    assert a.stream_tool_request.call_args.args[0] is PAYLOAD  # AIClient still applies caching itself


def test_router_switches_provider_live(env, monkeypatch):
    a = MagicMock()
    r = ModelRouter(a)
    S.save_ai_config("gpt-4o-mini", "sk-o", provider="openai")
    r.configure()
    assert r.provider == "openai"
    monkeypatch.setattr("urllib.request.OpenerDirector.open",
                        lambda self, req, timeout=None: _Resp(json.dumps({"choices": [{"message": {"content": "gpt"}}]}).encode()))
    assert r.make_tool_request(PAYLOAD)["content"][0]["text"] == "gpt"
    a.make_tool_request.assert_not_called()
    r.configure("anthropic")
    r.make_tool_request(PAYLOAD)
    a.make_tool_request.assert_called_once()


def test_anthropic_prompt_caching_intact():
    from src.services.ai_client import apply_prompt_caching
    out = apply_prompt_caching(json.loads(json.dumps(PAYLOAD)))
    assert "cache_control" in json.dumps(out)
