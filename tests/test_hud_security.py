"""Regression tests for audit items S1/S2: HUD server auth, Host/Origin, CORS, JSON-only, Claude-only."""
from unittest.mock import MagicMock

import pytest

import ui_server
from src.config import settings as settings_mod
from src.services.ai_client import AIClient, ANTHROPIC_MESSAGES_URL

PORT = 5000
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = BASE


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ui_server, "_HUD_PORT", PORT)
    # Never touch the real .env / status.json from tests.
    saved = MagicMock()
    monkeypatch.setattr(ui_server, "save_ai_config", saved)
    monkeypatch.setattr(ui_server, "_get_status_payload_str", lambda: '{"state": "Idle"}')
    monkeypatch.setattr(ui_server, "get_active_config", lambda: {"model": "claude-sonnet-5-5"})
    monkeypatch.setattr(ui_server, "_COMMAND_HANDLER", None)
    c = ui_server.flask_app.test_client()
    c.saved = saved
    return c


def auth(**extra):
    return {"X-HUD-Token": ui_server._HUD_TOKEN, **extra}


# --- token -----------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("get", "/status"), ("get", "/status.json"), ("get", "/config"),
    ("post", "/config"), ("post", "/test_config"), ("post", "/command"), ("post", "/clear_logs"),
])
def test_no_token_401(client, method, path):
    resp = getattr(client, method)(path, base_url=BASE, json={})
    assert resp.status_code == 401


def test_wrong_token_401(client):
    resp = client.get("/status", base_url=BASE, headers={"X-HUD-Token": "nope"})
    assert resp.status_code == 401


def test_token_only_in_index_page(client):
    page = client.get("/", base_url=BASE)
    assert page.status_code == 200
    assert ui_server._HUD_TOKEN in page.get_data(as_text=True)
    assert "__HUD_TOKEN__" not in page.get_data(as_text=True)
    status = client.get("/status", base_url=BASE, headers=auth())
    assert ui_server._HUD_TOKEN not in status.get_data(as_text=True)


# --- Host / Origin -----------------------------------------------------------

@pytest.mark.parametrize("base", ["http://evil.example:5000", "http://127.0.0.1:6000", "http://localhost"])
def test_wrong_host_403(client, base):
    assert client.get("/status", base_url=base, headers=auth()).status_code == 403
    assert client.get("/", base_url=base).status_code == 403


def test_localhost_host_allowed(client):
    resp = client.get("/status", base_url=f"http://localhost:{PORT}", headers=auth())
    assert resp.status_code == 200


@pytest.mark.parametrize("origin", ["http://evil.example", "null", f"http://localhost:{PORT}"])
def test_foreign_origin_403(client, origin):
    resp = client.post("/command", base_url=BASE, json={"command": "x"},
                       headers=auth(Origin=origin))
    assert resp.status_code == 403


def test_own_origin_allowed(client):
    resp = client.post("/command", base_url=BASE, json={"command": "hi"}, headers=auth(Origin=ORIGIN))
    assert resp.status_code == 200


# --- CORS ------------------------------------------------------------------

def test_no_cors_headers(client):
    for resp in (
        client.get("/status", base_url=BASE, headers=auth()),
        client.options("/command", base_url=BASE, headers=auth(Origin=ORIGIN)),
        client.get("/", base_url=BASE),
    ):
        assert not any(h.lower().startswith("access-control-") for h in resp.headers.keys())


# --- JSON only -------------------------------------------------------------

@pytest.mark.parametrize("path", ["/command", "/config", "/test_config"])
def test_form_encoded_post_415(client, path):
    resp = client.post(path, base_url=BASE, data={"command": "calc", "model": "x"}, headers=auth())
    assert resp.status_code == 415


def test_text_plain_post_415(client):
    resp = client.post("/command", base_url=BASE, data='{"command": "calc"}',
                       content_type="text/plain", headers=auth())
    assert resp.status_code == 415


# --- valid requests --------------------------------------------------------

def test_valid_command_200(client, monkeypatch):
    handler = MagicMock()
    monkeypatch.setattr(ui_server, "_COMMAND_HANDLER", handler)
    resp = client.post("/command", base_url=BASE, json={"command": "open notepad"},
                       headers=auth(Origin=ORIGIN))
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok", "command": "open notepad"}


def test_valid_status_200(client):
    resp = client.get("/status", base_url=BASE, headers=auth())
    assert resp.status_code == 200
    assert resp.get_json() == {"state": "Idle"}


def test_valid_config_post_200(client):
    resp = client.post("/config", base_url=BASE, headers=auth(),
                       json={"model": "claude-sonnet-5-5", "api_key": "sk-ant-test", "language": "English"})
    assert resp.status_code == 200
    client.saved.assert_called_once_with(model="claude-sonnet-5-5", api_key="sk-ant-test", language="English",
                                         provider=None, base_url=None)


def test_config_modal_has_no_arabic():
    import re, pathlib
    html = pathlib.Path(ui_server.UI_DIR / "index.html").read_text(encoding="utf-8")
    modal = html.split('id="settings-modal"')[1].split('id="memories-modal"')[0]
    assert not re.search(r"[؀-ۿ]", modal)


# --- Claude-only config ----------------------------------------------------

@pytest.mark.parametrize("path", ["/config", "/test_config"])
def test_config_rejects_unknown_provider_and_fields(client, path):
    for body in ({"provider": "evil"}, {"endpoint": "https://evil.example"}):
        resp = client.post(path, base_url=BASE, headers=auth(), json={"model": "m", **body})
        assert resp.status_code == 400
    client.saved.assert_not_called()


def test_config_rejects_newline_injection(client):
    resp = client.post("/config", base_url=BASE, headers=auth(),
                       json={"model": "claude-sonnet-5-5\nAI_API_KEY=x"})
    assert resp.status_code == 400
    client.saved.assert_not_called()


def test_anthropic_endpoint_ignores_base_url_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")
    assert AIClient().base_url == ANTHROPIC_MESSAGES_URL


def test_settings_endpoint_is_fixed(monkeypatch):
    monkeypatch.setenv("CUSTOM_BASE_URL", "https://evil.example")
    s = settings_mod.Settings()
    s.anthropic_base_url = "https://evil.example"  # legacy writers are ignored
    assert s.ai_base_url == s.anthropic_base_url == "https://api.anthropic.com"
    assert s.ai_provider == "anthropic"


def test_settings_default_models():
    assert settings_mod.DEFAULT_MODEL == "claude-sonnet-5-5"
    assert settings_mod.DEFAULT_FAST_MODEL == "claude-haiku-5-5"


def test_save_ai_config_writes_no_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("CUSTOM_BASE_URL", raising=False)
    env = tmp_path / ".env"
    settings_mod.save_ai_config("claude-sonnet-5-5", "sk-ant-x", "English", env_path=env)
    text = env.read_text(encoding="utf-8")
    assert "BASE_URL" not in text and "PROVIDER" not in text
    with pytest.raises(ValueError):
        settings_mod.save_ai_config("m\nAI_X=1", "", "English", env_path=env)


def test_ai_client_ignores_base_url_and_provider():
    c = AIClient(base_url="https://evil.example", api_key="k", model="", provider="openai")
    assert c._get_endpoint_url() == ANTHROPIC_MESSAGES_URL
    assert c.model == "claude-sonnet-5-5"
    c.update_config(provider="custom", base_url="http://evil.example", model="claude-haiku-5-5")
    assert c._get_endpoint_url() == ANTHROPIC_MESSAGES_URL
    assert c.model == "claude-haiku-5-5"


# --- API key handling (masked key bug) -------------------------------------

def _post_key(client, key):
    return client.post("/config", base_url=BASE, headers=auth(),
                       json={"model": "claude-sonnet-5-5", "api_key": key, "language": "English"})


@pytest.mark.parametrize("key", ["", "sk-a\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022", "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"])
def test_masked_or_empty_key_keeps_stored_key(tmp_path, monkeypatch, key):
    env = tmp_path / ".env"
    env.write_text("AI_API_KEY=sk-real-key-123\nANTHROPIC_API_KEY=sk-real-key-123\n", encoding="utf-8")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    real_save = settings_mod.save_ai_config
    monkeypatch.setattr(ui_server, "_HUD_PORT", PORT)
    monkeypatch.setattr(ui_server, "save_ai_config",
                        lambda **kw: real_save(env_path=env, **kw))
    monkeypatch.setattr(ui_server, "_CONFIG_UPDATED_CALLBACK", None)
    c = ui_server.flask_app.test_client()
    assert _post_key(c, key).status_code == 200
    text = env.read_text(encoding="utf-8")
    assert "AI_API_KEY=sk-real-key-123" in text
    assert "\u2022" not in text


@pytest.mark.parametrize("key", ["sk-\u00e9\u00e8\u00e0", "sk ant", "sk\tant"])
def test_invalid_key_400_and_not_echoed(client, key):
    resp = _post_key(client, key)
    assert resp.status_code == 400
    assert key not in resp.get_data(as_text=True)
    client.saved.assert_not_called()


def test_hot_reload_uses_real_key_not_payload(monkeypatch):
    import main
    monkeypatch.setattr(main, "Settings", lambda: MagicMock(ai_api_key="sk-real-key-123", ai_model="claude-sonnet-5-5"))
    ai = MagicMock()
    main.apply_ai_config(ai, {"model": "claude-sonnet-5-5", "api_key": "sk-a\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"})
    ai.update_config.assert_called_once_with(model="claude-sonnet-5-5", api_key="sk-real-key-123")
