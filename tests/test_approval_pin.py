"""PIN-gated approvals: direct execution on a valid PIN, 3-strike lockout, cancel, no PIN leakage,
and the Approve/Reject fallback when APPROVAL_PIN_HASH is unset. No network, no real .env."""
import logging

import pytest

import ui_server
from src.agent.approvals import APPROVED, DENIED, PENDING, ApprovalManager
from src.agent.tools import ToolResult
from src.security.pin import hash_pin, verify_pin
from src.services.telegram_service import TelegramApprovalBridge

PIN = "7391"
WRONG = "5555"
HASH = hash_pin(PIN, iterations=1000)  # few iterations: tests only
CHAT = OWNER = "1111"


def inline(fn):
    fn()


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, tool, args):
        self.calls.append((tool, args))
        return ToolResult(success=True, output={"message": "ran"})


class FakeApi:
    def __init__(self):
        self.calls = []

    def call(self, method, payload):
        self.calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 42}}

    def sent_text(self):
        return " ".join(str(p.get("text", "")) for m, p in self.calls if m == "sendMessage")


def make(pin_hash=HASH):
    runner = Runner()
    mgr = ApprovalManager(runner=runner, dispatch=inline, pin_hash=lambda: pin_hash)
    rid = mgr.create_request("terminal_run", {"cmd": "dir"}, channel="hud", summary="list files")
    return mgr, runner, rid


def tg_msg(text, chat=CHAT, user=OWNER):
    return {"chat": {"id": int(chat)}, "from": {"id": int(user)}, "text": text, "message_id": 7}


# --- hashing -------------------------------------------------------------------

def test_hash_is_salted_and_verifies():
    assert hash_pin(PIN) != hash_pin(PIN)
    assert verify_pin(PIN, HASH) and not verify_pin(WRONG, HASH)
    assert not verify_pin(PIN, "garbage") and not verify_pin(None, HASH)


# --- manager -------------------------------------------------------------------

def test_valid_pin_executes_immediately():
    mgr, runner, rid = make()
    res = mgr.verify_pin_and_approve(rid, PIN, via="hud")
    assert res.outcome == APPROVED
    assert runner.calls == [("terminal_run", {"cmd": "dir"})]


def test_three_wrong_pins_deny_and_a_late_right_pin_is_too_late():
    mgr, runner, rid = make()
    left = [mgr.verify_pin_and_approve(rid, WRONG, via="hud") for _ in range(3)]
    assert [r.outcome for r in left] == ["wrong_pin", "wrong_pin", "locked"]
    assert [r.attempts_left for r in left[:2]] == [2, 1]
    assert mgr.get(rid).status == DENIED
    assert mgr.verify_pin_and_approve(rid, PIN, via="hud").outcome == "already_used"
    assert runner.calls == []


def test_wrong_then_right_pin_still_runs_once():
    mgr, runner, rid = make()
    assert mgr.verify_pin_and_approve(rid, WRONG, via="hud").outcome == "wrong_pin"
    assert mgr.verify_pin_and_approve(rid, PIN, via="hud").outcome == APPROVED
    assert mgr.verify_pin_and_approve(rid, PIN, via="hud").outcome == "already_used"
    assert len(runner.calls) == 1


def test_cancel_denies_without_running():
    mgr, runner, rid = make()
    assert mgr.cancel(rid, "hud").outcome == DENIED
    assert mgr.get(rid).status == DENIED and runner.calls == []


def test_plain_approve_cannot_bypass_the_pin():
    mgr, runner, rid = make()
    res = mgr.resolve(rid, True, "hud", mgr.get(rid).args_hash)
    assert res.outcome == "pin_required" and mgr.get(rid).status == PENDING and runner.calls == []


def test_voice_can_never_approve_with_a_pin():
    mgr, runner, rid = make()
    assert mgr.verify_pin_and_approve(rid, PIN, via="voice").outcome == "forbidden_channel"
    assert runner.calls == []


def test_unset_pin_keeps_approve_reject_fallback():
    mgr, runner, rid = make(pin_hash="")
    assert mgr.pin_required is False
    assert mgr.list_pending()[0]["pin_required"] is False
    assert mgr.verify_pin_and_approve(rid, PIN, via="hud").outcome == "no_pin"
    assert mgr.resolve(rid, True, "hud", mgr.get(rid).args_hash).outcome == APPROVED
    assert len(runner.calls) == 1


# --- no leakage ----------------------------------------------------------------

def test_pins_never_reach_logs_or_stdout(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    secret_wrong, secret_right = "wr0ngPIN!", "r1ghtPIN!"
    runner = Runner()
    mgr = ApprovalManager(runner=runner, dispatch=inline, pin_hash=lambda: hash_pin(secret_right, iterations=1000))
    rid = mgr.create_request("t", {}, channel="telegram")
    api = FakeApi()
    bridge = TelegramApprovalBridge(mgr, api, CHAT, OWNER, dispatch=inline)
    bridge.send_request(mgr.get(rid))
    assert bridge.handle_message(tg_msg(secret_wrong)) is True
    assert bridge.handle_message(tg_msg(secret_right)) is True
    out = capsys.readouterr()
    for blob in (caplog.text, out.out, out.err, api.sent_text()):
        assert secret_wrong not in blob and secret_right not in blob
    assert runner.calls  # it did execute


# --- telegram ------------------------------------------------------------------

def test_telegram_pin_mode_has_no_buttons_and_pin_reply_runs_it():
    mgr, runner, _ = make()
    rid = mgr.create_request("terminal_run", {"cmd": "dir"}, channel="telegram", summary="list")
    api = FakeApi()
    bridge = TelegramApprovalBridge(mgr, api, CHAT, OWNER, dispatch=inline)
    bridge.send_request(mgr.get(rid))
    method, payload = api.calls[0]
    assert method == "sendMessage" and "reply_markup" not in payload
    assert "PIN" in payload["text"] and "cancel" in payload["text"] and "Args:" not in payload["text"]

    assert bridge.handle_message(tg_msg(PIN)) is True
    assert runner.calls == [("terminal_run", {"cmd": "dir"})]
    assert any(m == "deleteMessage" for m, _ in api.calls)  # the PIN message is removed from the chat


def test_telegram_wrong_pin_lockout_and_cancel_words():
    mgr, runner, _ = make()
    api = FakeApi()
    bridge = TelegramApprovalBridge(mgr, api, CHAT, OWNER, dispatch=inline)
    rid = mgr.create_request("t", {}, channel="telegram")
    bridge.send_request(mgr.get(rid))
    for _ in range(3):
        assert bridge.handle_message(tg_msg(WRONG)) is True
    assert mgr.get(rid).status == DENIED and "Too many" in api.sent_text()

    for word in ("cancel", "إلغاء"):
        rid = mgr.create_request("t", {}, channel="telegram")
        bridge.send_request(mgr.get(rid))
        assert bridge.handle_message(tg_msg(word)) is True
        assert mgr.get(rid).status == DENIED
    assert runner.calls == []


def test_telegram_ignores_strangers_and_non_pin_moments():
    mgr, _, _ = make()
    bridge = TelegramApprovalBridge(mgr, FakeApi(), CHAT, OWNER, dispatch=inline)
    assert bridge.handle_message(tg_msg(PIN)) is False  # nothing pending
    rid = mgr.create_request("t", {}, channel="telegram")
    bridge.send_request(mgr.get(rid))
    assert bridge.handle_message(tg_msg(PIN, chat="9", user="9")) is False
    assert mgr.get(rid).status == PENDING


def test_telegram_without_pin_still_sends_buttons():
    mgr, _, _ = make(pin_hash="")
    rid = mgr.create_request("t", {}, channel="telegram")
    api = FakeApi()
    bridge = TelegramApprovalBridge(mgr, api, CHAT, OWNER, dispatch=inline)
    bridge.send_request(mgr.get(rid))
    assert "inline_keyboard" in api.calls[0][1]["reply_markup"]
    assert bridge.handle_message(tg_msg("hello")) is False


# --- HUD endpoint --------------------------------------------------------------

@pytest.fixture
def hud(monkeypatch):
    monkeypatch.setattr(ui_server, "_HUD_PORT", 5000)
    mgr, runner, rid = make()
    monkeypatch.setattr(ui_server, "_APPROVAL_MANAGER", mgr)
    c = ui_server.flask_app.test_client()

    def post(body):
        return c.post("/approve", base_url="http://127.0.0.1:5000", json=body,
                      headers={"X-HUD-Token": ui_server._HUD_TOKEN})

    return mgr, runner, rid, post


def test_hud_pin_flow(hud):
    mgr, runner, rid, post = hud
    assert mgr.list_pending()[0]["pin_required"] is True
    r = post({"id": rid, "pin": WRONG})
    assert r.status_code == 403 and r.get_json()["attempts_left"] == 2
    assert mgr.list_pending()[0]["attempts_left"] == 2
    r = post({"id": rid, "pin": PIN})
    assert r.status_code == 200 and r.get_json()["status"] == "approved"
    assert len(runner.calls) == 1


def test_hud_lockout_and_approve_button_path_refused(hud):
    mgr, runner, rid, post = hud
    assert post({"id": rid, "decision": "approve", "args_hash": mgr.get(rid).args_hash}).status_code == 428
    codes = [post({"id": rid, "pin": WRONG}).status_code for _ in range(3)]
    assert codes == [403, 403, 423] and mgr.get(rid).status == DENIED and runner.calls == []


def test_hud_cancel_is_a_deny(hud):
    mgr, runner, rid, post = hud
    r = post({"id": rid, "decision": "deny", "args_hash": mgr.get(rid).args_hash})
    assert r.get_json()["status"] == "denied" and runner.calls == []
