"""HUD Security PIN management: POST /config/pin set / change / disable, and pin_enabled never leaks."""
import logging

import pytest

import ui_server
from src.config import settings as settings_mod
from src.security import pin as pin_mod

BASE = "http://127.0.0.1:5000"
OLD, NEW = "1234", "9876abcd"


@pytest.fixture
def hud(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("AI_MODEL=claude-sonnet-5-5\n", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "resolve_env_path", lambda data_dir=None: env)
    monkeypatch.setattr(pin_mod, "configured_hash", lambda: settings_mod.get_config_value("APPROVAL_PIN_HASH"))
    monkeypatch.setattr(pin_mod, "ITERATIONS", 1000)  # fast tests
    monkeypatch.setenv("APPROVAL_PIN_HASH", "")  # undone on teardown; update_env_file writes os.environ
    settings_mod.invalidate_config_cache()
    monkeypatch.setattr(ui_server, "_HUD_PORT", 5000)
    monkeypatch.setitem(ui_server._PIN_FAILS, "count", 0)
    monkeypatch.setitem(ui_server._PIN_FAILS, "until", 0.0)
    c = ui_server.flask_app.test_client()

    class H:
        path = env

        @staticmethod
        def pin(**body):
            return c.post("/config/pin", base_url=BASE, json=body, headers={"X-HUD-Token": ui_server._HUD_TOKEN})

        @staticmethod
        def config():
            return c.get("/config", base_url=BASE, headers={"X-HUD-Token": ui_server._HUD_TOKEN})

    return H


def stored():
    return pin_mod.configured_hash()


def test_set_pin_persists_hash_and_is_live(hud):
    assert hud.config().get_json()["pin_enabled"] is False
    r = hud.pin(action="set", new_pin=OLD)
    assert r.status_code == 200 and r.get_json() == {"status": "ok", "pin_enabled": True}
    assert pin_mod.verify_pin(OLD, stored())  # live, no restart
    text = hud.path.read_text(encoding="utf-8")
    assert "APPROVAL_PIN_HASH=pbkdf2_sha256$" in text and OLD not in text.split("APPROVAL_PIN_HASH=")[1]
    assert "AI_MODEL=claude-sonnet-5-5" in text  # unrelated settings survive


def test_pin_enabled_never_leaks_the_hash(hud):
    hud.pin(action="set", new_pin=OLD)
    body = hud.config().get_data(as_text=True)
    assert hud.config().get_json()["pin_enabled"] is True
    assert stored() not in body and "pbkdf2" not in body and OLD not in body
    assert "APPROVAL_PIN_HASH" not in body


def test_change_requires_the_current_pin(hud):
    hud.pin(action="set", new_pin=OLD)
    bad = hud.pin(action="change", current_pin="0000", new_pin=NEW)
    assert bad.status_code == 403 and pin_mod.verify_pin(OLD, stored())
    ok = hud.pin(action="change", current_pin=OLD, new_pin=NEW)
    assert ok.status_code == 200
    assert pin_mod.verify_pin(NEW, stored()) and not pin_mod.verify_pin(OLD, stored())


def test_disable_requires_the_current_pin(hud):
    hud.pin(action="set", new_pin=OLD)
    assert hud.pin(action="disable", current_pin="0000").status_code == 403
    assert hud.config().get_json()["pin_enabled"] is True
    r = hud.pin(action="disable", current_pin=OLD)
    assert r.get_json()["pin_enabled"] is False
    assert "APPROVAL_PIN_HASH" not in hud.path.read_text(encoding="utf-8")
    assert hud.config().get_json()["pin_enabled"] is False and stored() == ""


@pytest.mark.parametrize("body,code", [
    ({"action": "set", "new_pin": "123"}, 400),          # too short
    ({"action": "set", "new_pin": "1" * 13}, 400),       # too long
    ({"action": "set", "new_pin": 1234}, 400),           # not a string
    ({"action": "nope", "new_pin": OLD}, 400),
    ({"action": "change", "current_pin": OLD, "new_pin": NEW}, 409),  # nothing to change yet
    ({"action": "disable", "current_pin": OLD}, 409),
    ({"action": "set", "new_pin": OLD, "extra": 1}, 400),
])
def test_validation(hud, body, code):
    assert hud.pin(**body).status_code == code


def test_set_when_already_set_is_refused(hud):
    hud.pin(action="set", new_pin=OLD)
    assert hud.pin(action="set", new_pin=NEW).status_code == 409
    assert pin_mod.verify_pin(OLD, stored())


def test_wrong_current_pin_locks_out_after_five(hud):
    hud.pin(action="set", new_pin=OLD)
    assert [hud.pin(action="disable", current_pin="0000").status_code for _ in range(5)] == [403] * 5
    assert hud.pin(action="disable", current_pin=OLD).status_code == 429  # even the right PIN waits
    assert pin_mod.verify_pin(OLD, stored())


def test_endpoint_needs_the_hud_token_and_json(hud):
    c = ui_server.flask_app.test_client()
    assert c.post("/config/pin", base_url=BASE, json={"action": "set", "new_pin": OLD}).status_code == 401
    r = c.post("/config/pin", base_url=BASE, data="x", headers={"X-HUD-Token": ui_server._HUD_TOKEN})
    assert r.status_code == 415


def test_pins_never_logged(hud, caplog):
    caplog.set_level(logging.DEBUG)
    hud.pin(action="set", new_pin="s3cretPIN")
    hud.pin(action="change", current_pin="wr0ngPIN!", new_pin="n3wSecret")
    hud.pin(action="change", current_pin="s3cretPIN", new_pin="n3wSecret")
    for raw in ("s3cretPIN", "wr0ngPIN!", "n3wSecret"):
        assert raw not in caplog.text
