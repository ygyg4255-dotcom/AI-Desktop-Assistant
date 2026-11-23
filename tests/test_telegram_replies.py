"""Regression: normal Telegram commands and post-PIN tool results must reach Telegram."""
import json
from unittest import mock

import main
from src.agent.approvals import ApprovalManager
from src.agent.tools import ToolResult, create_default_registry
from src.agent.permissions import PermissionGate
from src.services import telegram_service as tg

CHAT = "1111"
TOKEN = "123456789:AAEhBP0av28vWbRYVqzcAbCdEfGhIjKlMnO"


def _updates(*texts):
    return json.dumps({"ok": True, "result": [
        {"update_id": i + 1, "message": {"chat": {"id": int(CHAT)}, "from": {"id": 1}, "text": t, "message_id": i}}
        for i, t in enumerate(texts)]}).encode()


def _run_listener(monkeypatch, tmp_path, body, interceptor):
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path))
    lst = tg.TelegramRemoteListener(TOKEN, CHAT, message_interceptor=interceptor)
    got = []
    lst.remote_command_received.connect(got.append)

    polls = []

    def fake_open(req, timeout=0):
        polls.append(1)
        if len(polls) > 1:  # second poll: stop the loop
            lst.stop()
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = body
        return resp
    monkeypatch.setattr(tg.urllib.request, "urlopen", fake_open)
    lst.run()
    return got


def test_normal_commands_pass_when_interceptor_declines(monkeypatch, tmp_path):
    assert _run_listener(monkeypatch, tmp_path, _updates("Hi"), lambda m: False) == ["Hi"]


def test_pin_reply_is_consumed_not_emitted(monkeypatch, tmp_path):
    assert _run_listener(monkeypatch, tmp_path, _updates("1234"), lambda m: True) == []


def test_broken_interceptor_never_swallows_commands(monkeypatch, tmp_path):
    def boom(m):
        raise RuntimeError("x")
    assert _run_listener(monkeypatch, tmp_path, _updates("Hi"), boom) == ["Hi"]


def test_post_pin_tool_result_is_sent_to_telegram():
    sent = []
    mgr = ApprovalManager(runner=lambda t, a: ToolResult(success=True, output={"message": "deleted test.py"}),
                          dispatch=lambda fn: fn(), pin_hash=lambda: None)
    mgr.add_listener(main.make_approval_listener([mock.Mock()], notify_telegram=sent.append))
    rid = mgr.create_request("file_delete", {"path": "x"}, channel="telegram", summary="s")
    mgr.resolve(rid, True, "telegram", mgr.get(rid).args_hash)
    assert any("deleted test.py" in s for s in sent)


def test_file_list_is_safe_and_lists(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    assert "file_list" in PermissionGate.SAFE_TOOLS
    res = create_default_registry().get("file_list").run({"directory": str(tmp_path)})
    assert res.success and {e["name"] for e in res.output["entries"]} == {"a.txt", "sub"}
