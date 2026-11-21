"""claude_code_run: CLI and VS Code are always mocked."""
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agent import tools as tools_mod
from src.agent.permissions import PermissionGate
from src.services import claude_code_service as svc


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(svc.shutil, "which",
                        lambda n: {"claude": "C:/bin/claude.cmd", "code": "C:/bin/code.cmd"}.get(n))
    run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="done", stderr=""))
    popen = MagicMock()
    monkeypatch.setattr(svc.subprocess, "run", run)
    monkeypatch.setattr(svc.subprocess, "Popen", popen)
    return SimpleNamespace(home=home, run=run, popen=popen)


def test_binary_detection_path_and_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(svc.shutil, "which", lambda n: "C:/x/claude.exe" if n == "claude" else None)
    assert svc.find_claude_binary() == "C:/x/claude.exe"
    monkeypatch.setattr(svc.shutil, "which", lambda n: None)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    assert svc.find_claude_binary() is None
    (tmp_path / "npm").mkdir()
    (tmp_path / "npm" / "claude.cmd").write_text("")
    assert svc.find_claude_binary() == str(tmp_path / "npm" / "claude.cmd")


def test_missing_binary_returns_error(env, monkeypatch):
    monkeypatch.setattr(svc.shutil, "which", lambda n: None)
    monkeypatch.setattr(svc, "find_claude_binary", lambda: None)
    out = svc.run_claude_code("x")
    assert not out["ok"] and "not found" in out["output"]
    env.run.assert_not_called()


def test_creates_desktop_folder(env):
    out = svc.run_claude_code("build app", project_path="my-app")
    target = env.home / "Desktop" / "my-app"
    assert target.is_dir()
    assert out["ok"] and out["project_path"] == str(target)
    assert env.run.call_args.kwargs["cwd"] == str(target)


def test_missing_dir_without_create(env):
    out = svc.run_claude_code("x", project_path="ghost", create_if_missing=False)
    assert not out["ok"] and "does not exist" in out["output"]
    assert not (env.home / "Desktop" / "ghost").exists()
    env.run.assert_not_called()


def test_command_flags_and_no_shell(env):
    svc.run_claude_code("do it", project_path="p")
    cmd = env.run.call_args.args[0]
    kw = env.run.call_args.kwargs
    assert cmd[:2] == ["C:/bin/claude.cmd", "-p"] and cmd[2] == "do it"
    assert "--permission-mode" in cmd and cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert cmd[cmd.index("--output-format") + 1] == "text"
    assert not kw.get("shell")
    assert kw["encoding"] == "utf-8" and kw["errors"] == "replace"
    assert kw["timeout"] == 300


def test_open_in_vscode(env):
    out = svc.run_claude_code("x", project_path="p", open_in_vscode=True)
    assert out["opened_in_vscode"]
    assert env.popen.call_args.args[0] == ["C:/bin/code.cmd", str(env.home / "Desktop" / "p")]


def test_no_vscode_on_failure_or_flag_off(env):
    svc.run_claude_code("x", project_path="p")
    env.popen.assert_not_called()
    env.run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="boom")
    out = svc.run_claude_code("x", project_path="p", open_in_vscode=True)
    assert not out["ok"] and "boom" in out["output"] and not out["opened_in_vscode"]
    env.popen.assert_not_called()


def test_timeout(env):
    env.run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=5)
    out = svc.run_claude_code("x", project_path="p", timeout_seconds=5)
    assert not out["ok"] and "timed out" in out["output"]


def test_output_truncated(env):
    env.run.return_value = SimpleNamespace(returncode=0, stdout="a" * 10000, stderr="")
    out = svc.run_claude_code("x", project_path="p")
    assert len(out["output"]) < 4000 and out["output"].endswith("[output truncated]")


def test_default_path_is_cwd(env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = svc.run_claude_code("x")
    assert Path(out["project_path"]).resolve() == tmp_path.resolve()


def test_tool_registered_and_safe():
    reg = tools_mod.create_default_registry()
    tool = reg.get("claude_code_run") if hasattr(reg, "get") else None
    assert tool is not None
    assert "claude_code_run" in PermissionGate.SAFE_TOOLS


def test_vscode_alias():
    for name in ("vscode", "vs code"):
        assert tools_mod.APP_NAME_ALIASES[name] == "code"
