"""Regression tests for audit item S5a: out-of-band approval flow for dangerous tool calls.

No network: Telegram is replaced by fakes, AI responses by a MagicMock.
"""
import json
import threading
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt

import ui_server
from src.agent import executor as executor_mod
from src.agent.approvals import (
    ALREADY_USED, APPROVED, DENIED, EXPIRED, FORBIDDEN_CHANNEL, HASH_MISMATCH, PENDING, UNKNOWN,
    ApprovalManager, args_hash,
)
from src.agent.executor import AgentExecutor
from src.agent.memory import ConversationMemory
from src.agent.tools import Tool, ToolCategory, ToolParameter, ToolResult
from src.services import telegram_service
from src.services.telegram_service import TelegramApprovalBridge, TelegramRemoteWorker

CHAT_ID = "1111"
OWNER_ID = "1111"


def inline(fn):
    fn()


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


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
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 42}}
        return {"ok": True}

    def methods(self):
        return [m for m, _ in self.calls]

    def last(self, method):
        return [p for m, p in self.calls if m == method][-1]


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def runner():
    return Runner()


@pytest.fixture
def manager(clock, runner):
    return ApprovalManager(runner=runner, ttl=120, clock=clock, dispatch=inline)


@pytest.fixture
def bridge(manager):
    api = FakeApi()
    b = TelegramApprovalBridge(manager, api, chat_id=CHAT_ID, owner_user_id=OWNER_ID, dispatch=inline)
    manager.add_listener(lambda ev, req: b.on_resolved(req) if ev == "resolved" else
                         (b.send_request(req) if ev == "created" else None))
    b.api = api
    return b


def callback(req, action="a", chat_id=CHAT_ID, user_id=OWNER_ID, hash_prefix=None, cq_id="cq1"):
    return {
        "id": cq_id,
        "from": {"id": int(user_id)},
        "message": {"message_id": 42, "chat": {"id": int(chat_id)}},
        "data": f"ap:{action}:{req.id}:{hash_prefix or req.args_hash[:16]}",
    }


def new_request(manager, channel="telegram", args=None):
    rid = manager.create_request("file_write", args or {"path": "C:/x.txt", "content": "hi"}, channel,
                                 summary="Create file: C:/x.txt")
    return manager.get(rid)


# --- ApprovalManager core ----------------------------------------------------

def test_ids_are_unguessable_and_unique(manager):
    ids = {new_request(manager).id for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) >= 20 for i in ids)


def test_single_use_nonce(manager, runner):
    req = new_request(manager, channel="hud")
    first = manager.resolve(req.id, True, via="hud", expected_hash=req.args_hash)
    second = manager.resolve(req.id, True, via="hud", expected_hash=req.args_hash)
    third = manager.resolve(req.id, False, via="hud", expected_hash=req.args_hash)
    assert first.outcome == APPROVED
    assert second.outcome == ALREADY_USED and third.outcome == ALREADY_USED
    assert len(runner.calls) == 1


def test_expired_request_denies(manager, runner, clock):
    req = new_request(manager, channel="hud")
    clock.now += 121
    res = manager.resolve(req.id, True, via="hud", expected_hash=req.args_hash)
    assert res.outcome == EXPIRED
    assert req.status == EXPIRED
    assert runner.calls == []
    assert manager.list_pending() == []


def test_sweep_expires_without_any_resolve(manager, runner, clock):
    req = new_request(manager, channel="hud")
    events = []
    manager.add_listener(lambda ev, r: events.append((ev, r.status)))
    clock.now += 120
    manager.sweep()
    assert req.status == EXPIRED and ("resolved", EXPIRED) in events
    assert runner.calls == []


def test_args_hash_mismatch_denies(manager, runner):
    req = new_request(manager, channel="hud")
    other = args_hash("file_write", {"path": "C:/Windows/evil.txt", "content": "hi"})
    res = manager.resolve(req.id, True, via="hud", expected_hash=other)
    assert res.outcome == HASH_MISMATCH
    assert req.status == DENIED
    assert runner.calls == []
    # and it is consumed: a later correct-hash approval does nothing
    assert manager.resolve(req.id, True, via="hud", expected_hash=req.args_hash).outcome == ALREADY_USED
    assert runner.calls == []


def test_short_hash_prefix_rejected(manager, runner):
    req = new_request(manager, channel="hud")
    assert manager.resolve(req.id, True, via="hud", expected_hash=req.args_hash[:4]).outcome == HASH_MISMATCH
    assert runner.calls == []


def test_deny_works(manager, runner):
    req = new_request(manager, channel="hud")
    res = manager.resolve(req.id, False, via="hud", expected_hash=req.args_hash)
    assert res.outcome == DENIED and req.status == DENIED
    assert runner.calls == []


def test_unknown_id(manager):
    assert manager.resolve("nope", True, via="hud", expected_hash="0" * 64).outcome == UNKNOWN


def test_approved_call_runs_with_exactly_the_shown_args(manager, runner):
    args = {"path": "C:/x.txt", "content": "hi", "nested": {"k": [1, 2]}}
    req = new_request(manager, channel="hud", args=args)
    shown = manager.list_pending()[0]
    # caller mutates its dict after the request was shown: must not affect what runs
    args["path"] = "C:/Windows/System32/evil.dll"
    args["nested"]["k"].append(3)
    manager.resolve(shown["id"], True, via="hud", expected_hash=shown["args_hash"])
    assert runner.calls == [("file_write", {"path": "C:/x.txt", "content": "hi", "nested": {"k": [1, 2]}})]
    assert shown["args_hash"] == args_hash("file_write", runner.calls[0][1])


def test_tampered_stored_args_do_not_run(manager, runner):
    req = new_request(manager, channel="hud")
    req.args["path"] = "C:/evil"  # simulate in-memory tampering after hashing
    manager.resolve(req.id, True, via="hud", expected_hash=req.args_hash)
    assert runner.calls == []
    assert req.meta["run_error"] == "integrity check failed"


def test_voice_origin_cannot_self_approve(manager, runner):
    req = new_request(manager, channel="voice")
    assert "voice" not in req.approver_channels
    res = manager.resolve(req.id, True, via="voice", expected_hash=req.args_hash)
    assert res.outcome == FORBIDDEN_CHANNEL
    assert req.status == PENDING and runner.calls == []
    # but the owner can approve it on screen
    assert manager.resolve(req.id, True, via="hud", expected_hash=req.args_hash).outcome == APPROVED
    assert len(runner.calls) == 1


def test_no_origin_ever_allows_voice_approval(manager):
    for channel in ("local", "hud", "telegram", "voice", "something-else"):
        assert "voice" not in new_request(manager, channel=channel).approver_channels


def test_hud_origin_cannot_be_approved_from_telegram(manager, runner):
    req = new_request(manager, channel="hud")
    assert manager.resolve(req.id, True, via="telegram", expected_hash=req.args_hash).outcome == FORBIDDEN_CHANNEL
    assert runner.calls == []


def test_concurrent_approvals_run_once(manager, runner):
    req = new_request(manager, channel="hud")
    barrier = threading.Barrier(8)
    outcomes = []

    def _go():
        barrier.wait()
        outcomes.append(manager.resolve(req.id, True, via="hud", expected_hash=req.args_hash).outcome)

    threads = [threading.Thread(target=_go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count(APPROVED) == 1
    assert len(runner.calls) == 1


# --- Telegram bridge ---------------------------------------------------------

def test_telegram_request_has_inline_keyboard_and_truncated_args(manager, bridge):
    req = new_request(manager, channel="telegram", args={"path": "C:/x.txt", "content": "A" * 5000})
    method, payload = bridge.api.calls[0]
    assert method == "sendMessage" and payload["chat_id"] == CHAT_ID
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["✅ Approve", "❌ Deny"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)
    assert all(req.id in b["callback_data"] for b in buttons)
    assert "file_write" in payload["text"] and len(payload["text"]) < 1000


def test_telegram_approve_answers_and_edits(manager, bridge, runner):
    req = new_request(manager)
    assert bridge.handle_callback_query(callback(req, "a")) == APPROVED
    assert len(runner.calls) == 1
    assert "answerCallbackQuery" in bridge.api.methods()
    assert bridge.api.last("editMessageText")["text"].endswith("✅ Approved")
    assert bridge.api.last("editMessageText")["message_id"] == 42


def test_telegram_deny_answers_and_edits(manager, bridge, runner):
    req = new_request(manager)
    assert bridge.handle_callback_query(callback(req, "d")) == DENIED
    assert runner.calls == []
    assert bridge.api.last("editMessageText")["text"].endswith("❌ Denied")


def test_telegram_expired_edits_message(manager, bridge, runner, clock):
    req = new_request(manager)
    clock.now += 500
    assert bridge.handle_callback_query(callback(req, "a")) == EXPIRED
    assert runner.calls == []
    assert "Expired" in bridge.api.last("editMessageText")["text"]
    assert bridge.api.last("answerCallbackQuery")["text"] == "This request has expired."


def test_telegram_wrong_chat_id_rejected(manager, bridge, runner):
    req = new_request(manager)
    assert bridge.handle_callback_query(callback(req, "a", chat_id="2222")) == "forbidden"
    assert req.status == PENDING and runner.calls == []
    assert bridge.api.last("answerCallbackQuery")["text"] == "Not authorized."


def test_telegram_wrong_user_id_rejected(manager, bridge, runner):
    """Right chat (e.g. a group the owner is in) but a different sender."""
    req = new_request(manager)
    assert bridge.handle_callback_query(callback(req, "a", user_id="3333")) == "forbidden"
    assert req.status == PENDING and runner.calls == []


def test_telegram_used_nonce_rejected(manager, bridge, runner):
    req = new_request(manager)
    bridge.handle_callback_query(callback(req, "a"))
    assert bridge.handle_callback_query(callback(req, "a", cq_id="cq2")) == ALREADY_USED
    assert len(runner.calls) == 1


def test_telegram_hash_mismatch_rejected(manager, bridge, runner):
    req = new_request(manager)
    assert bridge.handle_callback_query(callback(req, "a", hash_prefix="0" * 16)) == HASH_MISMATCH
    assert runner.calls == []


def test_telegram_garbage_data_rejected(manager, bridge, runner):
    req = new_request(manager)
    cq = callback(req)
    cq["data"] = "rm -rf"
    assert bridge.handle_callback_query(cq) == "unknown"
    assert req.status == PENDING


# --- HUD endpoints -------------------------------------------------------------

PORT = 5000
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def hud(monkeypatch, manager):
    monkeypatch.setattr(ui_server, "_HUD_PORT", PORT)
    monkeypatch.setattr(ui_server, "_APPROVAL_MANAGER", manager)
    return ui_server.flask_app.test_client()


def auth(**extra):
    return {"X-HUD-Token": ui_server._HUD_TOKEN, **extra}


@pytest.mark.parametrize("method,path", [("get", "/approvals"), ("post", "/approve")])
def test_approval_endpoints_401_without_token(hud, manager, runner, method, path):
    req = new_request(manager, channel="hud")
    body = {"id": req.id, "decision": "approve", "args_hash": req.args_hash}
    resp = getattr(hud, method)(path, base_url=BASE, json=body)
    assert resp.status_code == 401
    resp = getattr(hud, method)(path, base_url=BASE, json=body, headers={"X-HUD-Token": "wrong"})
    assert resp.status_code == 401
    assert runner.calls == [] and req.status == PENDING


def test_approve_wrong_host_and_origin_403(hud, manager, runner):
    req = new_request(manager, channel="hud")
    body = {"id": req.id, "decision": "approve", "args_hash": req.args_hash}
    assert hud.post("/approve", base_url="http://evil.example:5000", json=body, headers=auth()).status_code == 403
    assert hud.post("/approve", base_url=BASE, json=body, headers=auth(Origin="http://evil.example")).status_code == 403
    assert runner.calls == []


def test_approve_requires_json(hud, manager, runner):
    req = new_request(manager, channel="hud")
    resp = hud.post("/approve", base_url=BASE, headers=auth(),
                    data={"id": req.id, "decision": "approve", "args_hash": req.args_hash})
    assert resp.status_code == 415
    assert runner.calls == []


def test_hud_list_and_approve_runs_once(hud, manager, runner):
    new_request(manager, channel="hud")
    listed = hud.get("/approvals", base_url=BASE, headers=auth()).get_json()["pending"]
    assert len(listed) == 1 and listed[0]["tool"] == "file_write"
    item = listed[0]
    body = {"id": item["id"], "decision": "approve", "args_hash": item["args_hash"]}
    first = hud.post("/approve", base_url=BASE, json=body, headers=auth())
    second = hud.post("/approve", base_url=BASE, json=body, headers=auth())
    assert first.status_code == 200 and first.get_json()["status"] == APPROVED
    assert second.status_code == 409 and second.get_json()["status"] == ALREADY_USED
    assert len(runner.calls) == 1
    assert hud.get("/approvals", base_url=BASE, headers=auth()).get_json()["pending"] == []


def test_hud_deny_and_bad_payloads(hud, manager, runner):
    req = new_request(manager, channel="hud")
    bad = [
        {"id": req.id, "decision": "yes", "args_hash": req.args_hash},
        {"id": req.id, "decision": "approve"},
        {"id": 5, "decision": "approve", "args_hash": req.args_hash},
        {"id": req.id, "decision": "approve", "args_hash": req.args_hash, "extra": 1},
    ]
    for body in bad:
        assert hud.post("/approve", base_url=BASE, json=body, headers=auth()).status_code == 400
    resp = hud.post("/approve", base_url=BASE, headers=auth(),
                    json={"id": req.id, "decision": "deny", "args_hash": req.args_hash})
    assert resp.status_code == 200 and resp.get_json()["status"] == DENIED
    assert runner.calls == []


# --- executor integration --------------------------------------------------------

class DangerTool:
    """A gated tool that counts real (approved) executions."""

    def __init__(self):
        self.runs = []

    def handler(self, params, approved=False):
        if not approved:
            return ToolResult(success=False, output=None, requires_permission=True,
                              permission_message=f"Danger on {params.get('path')}")
        self.runs.append(dict(params))
        return ToolResult(success=True, output={"message": "danger done"})


@pytest.fixture
def danger():
    return DangerTool()


@pytest.fixture
def agent(monkeypatch, tmp_path, danger):
    monkeypatch.setattr(executor_mod, "update_hud_status", lambda *a, **k: None)
    ai = MagicMock()
    mgr = ApprovalManager(dispatch=inline)
    ex = AgentExecutor(ai_client=ai, database=None, approvals=mgr,
                       memory=ConversationMemory(memory_file=tmp_path / "memory.json"))
    ex.registry.register(Tool(
        name="fake_danger", description="test", category=ToolCategory.UTILITY,
        parameters=[ToolParameter(name="path", type="string", description="p")],
        handler=danger.handler, requires_permission=True,
    ))
    ex.ai = ai
    return ex


def tool_use(name, inp):
    return {"content": [{"type": "tool_use", "id": "t1", "name": name, "input": inp}]}


def text_reply(text):
    return {"content": [{"type": "text", "text": text}]}


def test_llm_path_creates_request_without_blocking_and_runs_once(agent, danger):
    agent.ai.make_tool_request.return_value = tool_use("fake_danger", {"path": "C:/a", "_approved": True})
    done = []
    t = threading.Thread(target=lambda: done.append(agent._process_sync("do it", origin="hud")))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "_process_sync blocked waiting for approval"
    resp = done[0]
    assert "approval" in resp.content.lower() and danger.runs == []

    pending = agent.approvals.list_pending()
    assert len(pending) == 1 and pending[0]["channel"] == "hud"
    # internal "_approved" key is stripped from what is shown and what runs
    assert "_approved" not in pending[0]["args_preview"]
    agent.approvals.resolve(pending[0]["id"], True, via="hud", expected_hash=pending[0]["args_hash"])
    agent.approvals.resolve(pending[0]["id"], True, via="hud", expected_hash=pending[0]["args_hash"])
    assert danger.runs == [{"path": "C:/a"}], "tool must run exactly once with the shown args"


def test_local_intent_requires_approval_is_shown_not_dropped(agent, danger, monkeypatch):
    monkeypatch.setattr(executor_mod, "match_local_intent", lambda p: {
        "tool_name": "fake_danger", "tool_input": {"path": "C:/b"}, "confirmation": "ok"})
    resp = agent._process_sync("open the thing", origin="telegram")
    assert "Waiting for your approval" in resp.content
    assert "Action failed" not in resp.content
    pending = agent.approvals.list_pending()
    assert len(pending) == 1 and pending[0]["channel"] == "telegram"
    assert danger.runs == []
    agent.ai.make_tool_request.assert_not_called()


def test_local_intent_without_manager_still_visible(monkeypatch, tmp_path, danger):
    monkeypatch.setattr(executor_mod, "update_hud_status", lambda *a, **k: None)
    ex = AgentExecutor(ai_client=MagicMock(), database=None,
                       memory=ConversationMemory(memory_file=tmp_path / "m.json"))
    ex.registry.register(Tool(name="fake_danger", description="t", category=ToolCategory.UTILITY,
                              parameters=[], handler=danger.handler, requires_permission=True))
    monkeypatch.setattr(executor_mod, "match_local_intent", lambda p: {
        "tool_name": "fake_danger", "tool_input": {"path": "C:/b"}, "confirmation": "ok"})
    resp = ex._process_sync("x")
    assert "needs approval and was not run" in resp.content and danger.runs == []


def test_voice_origin_says_waiting_on_screen(agent, danger):
    agent.ai.make_tool_request.return_value = tool_use("fake_danger", {"path": "C:/v"})
    resp = agent._process_sync("delete it", origin="voice")
    assert "on screen" in resp.content and "not by voice" in resp.content
    req = agent.approvals.get(agent.approvals.list_pending()[0]["id"])
    assert agent.approvals.resolve(req.id, True, via="voice", expected_hash=req.args_hash).outcome == FORBIDDEN_CHANNEL
    assert danger.runs == []


# --- Telegram listener keeps processing while a request is pending --------------------

class FakeResp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_listener_keeps_processing_while_request_pending(agent, danger, monkeypatch):
    """Command 1 creates a pending approval; the listener must still handle command 2
    and the callbacks in later polls, and the tool runs once only after approval."""
    agent.ai.make_tool_request.side_effect = [
        tool_use("fake_danger", {"path": "C:/t"}),
        text_reply("It is noon."),
    ]
    api = FakeApi()
    bridge = TelegramApprovalBridge(agent.approvals, api, chat_id=CHAT_ID, owner_user_id=OWNER_ID, dispatch=inline)
    agent.approvals.add_listener(lambda ev, r: bridge.send_request(r) if ev == "created" else
                                 (bridge.on_resolved(r) if ev == "resolved" else None))

    worker = TelegramRemoteWorker(bot_token="test-token", allowed_chat_id=CHAT_ID,
                                  callback_handler=bridge.handle_callback_query)
    replies, pending_seen = [], []

    def on_command(text):
        pending_seen.append(len(agent.approvals.list_pending()))
        replies.append(agent._process_sync(text, origin="telegram").content)

    # main.py connects with DirectConnection: the slot runs on the listener thread.
    worker.remote_command_received.connect(on_command, Qt.ConnectionType.DirectConnection)

    msg = lambda uid, text: {"update_id": uid, "message": {"chat": {"id": int(CHAT_ID)}, "text": text}}
    polls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        polls["n"] += 1
        if polls["n"] == 1:
            return FakeResp({"ok": True, "result": [msg(1, "delete C:/t")]})
        if polls["n"] == 2:
            req_id = agent.approvals.list_pending()[0]["id"]
            r = agent.approvals.get(req_id)
            return FakeResp({"ok": True, "result": [
                msg(2, "what time is it"),
                {"update_id": 3, "callback_query": callback(r, "a", user_id="9999", cq_id="bad")},
            ]})
        if polls["n"] == 3:
            assert danger.runs == [], "tool ran before the owner approved"
            r = agent.approvals.get(agent.approvals.list_pending()[0]["id"])
            return FakeResp({"ok": True, "result": [
                {"update_id": 4, "callback_query": callback(r, "a", cq_id="good")},
                {"update_id": 5, "callback_query": callback(r, "a", cq_id="replay")},
            ]})
        worker.stop()
        return FakeResp({"ok": True, "result": []})

    monkeypatch.setattr(telegram_service.urllib.request, "urlopen", fake_urlopen)

    t = threading.Thread(target=worker.run)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "Telegram listener blocked"

    assert len(replies) == 2
    assert "approval" in replies[0].lower()
    assert replies[1] == "It is noon."
    assert pending_seen == [0, 1], "second command was processed while the request was pending"
    assert danger.runs == [{"path": "C:/t"}]
    answers = [p["text"] for m, p in api.calls if m == "answerCallbackQuery"]
    assert answers == ["Not authorized.", "Approved — running now.", "This request was already handled."]
    assert worker._offset == 6
