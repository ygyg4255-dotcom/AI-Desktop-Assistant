"""Plain-language approval previews, Recycle Bin deletion (mocked Shell API) and file_restore."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.agent import tools
from src.agent.approvals import ApprovalManager, describe_action
from src.agent.permissions import PermissionGate
from src.services.telegram_service import TelegramApprovalBridge

ROOT = Path(__file__).resolve().parents[1]


def test_describe_action_plain_language():
    assert describe_action("file_delete", {"path": "~/Desktop/test.py"}) == "Move `test.py` from Desktop to Recycle Bin"
    assert describe_action("power_control", {"action": "restart"}) == "Restart the PC"
    assert describe_action("terminal_execute", {"command": "Get-Process", "summary": "List running programs"}) == "List running programs"
    fb = describe_action("terminal_execute", {"command": "x" * 200})
    assert fb.startswith("Run a terminal command: ") and len(fb) < 120
    assert describe_action("claude_code_run", {"prompt": "build a todo app"}).startswith("Run a coding task")


def _req(tool, args):
    mgr = ApprovalManager(pin_hash=lambda: "")
    return mgr.get(mgr.create_request(tool, args, "telegram", summary="Run command: raw"))


def test_telegram_shows_summary_not_raw_args():
    code = "Get-ChildItem C:\\secret | Remove-Item -Recurse"
    text = TelegramApprovalBridge.request_text(
        _req("terminal_execute", {"command": code, "summary": "Delete the temp folder"}))
    assert "Delete the temp folder" in text and "terminal_execute" in text
    assert "Args:" not in text and "Remove-Item" not in text


def test_hud_view_has_summary_and_technical_details():
    req = _req("terminal_execute", {"command": "dir", "summary": "List files"})
    view = req.public_view(0)
    assert view["summary"] == "List files" and "dir" in view["args_preview"]
    js = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
    assert "Show technical details" in js and "createElement('details')" in js


def test_file_delete_uses_recycle_bin(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    with mock.patch.object(tools, "_recycle") as rec:
        res = tools.handle_file_delete({"path": str(f)}, approved=True)
    rec.assert_called_once()
    assert res.success and "Recycle Bin" in res.output["message"]
    assert f.exists()  # the mocked Shell call did not touch the file; no permanent delete happened


def test_file_delete_reports_shell_failure(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    with mock.patch.object(tools, "_recycle", side_effect=OSError("boom")):
        res = tools.handle_file_delete({"path": str(f)}, approved=True)
    assert not res.success and "boom" in res.error


def test_file_restore_ok_not_found_and_invalid():
    ok = SimpleNamespace(returncode=0, stdout="C:\\Users\\me\\Desktop\\a.txt\n", stderr="")
    missing = SimpleNamespace(returncode=3, stdout="", stderr="")
    with mock.patch.object(tools.platform, "system", return_value="Windows"), \
         mock.patch.object(tools.shutil, "which", return_value="powershell"), \
         mock.patch.object(tools.subprocess, "run", return_value=ok) as run:
        res = tools.handle_file_restore({"name": "a.txt"})
        assert res.success and res.output["restored"].endswith("a.txt")
        assert run.call_args.kwargs["env"]["ALTRON_RESTORE"] == "a.txt"  # name never enters the script text
    with mock.patch.object(tools.platform, "system", return_value="Windows"), \
         mock.patch.object(tools.shutil, "which", return_value="powershell"), \
         mock.patch.object(tools.subprocess, "run", return_value=missing):
        res = tools.handle_file_restore({"name": "ghost.txt"})
        assert not res.success and "not found" in res.error
    assert not tools.handle_file_restore({"name": "*"}).success


def test_file_restore_registered_and_safe():
    assert tools.create_default_registry().get("file_restore") is not None
    assert PermissionGate().is_safe("file_restore")


def test_prompt_mentions_restore_and_summary_within_cap():
    text = (ROOT / "prompts" / "system_prompt.txt").read_text(encoding="utf-8")
    assert "file_restore" in text and "`summary`" in text and len(text) < 6900
