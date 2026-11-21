"""Delegate coding tasks to the local Claude Code CLI (non-interactive, no shell)."""
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from src.config.log import get_logger, redact_secrets

logger = get_logger(__name__)

MAX_OUTPUT_CHARS = 3500
MAX_LOGGED_STDERR_CHARS = 1500
_TRUNCATION_NOTE = "\n...[output truncated]"

# Altron's own Anthropic overrides (loaded from its .env) must not leak into the
# Claude Code CLI: a local ANTHROPIC_BASE_URL makes it fail with ECONNREFUSED, and
# the CLI should use its own native auth and default model.
_STRIPPED_ENV_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")


def build_cli_env() -> Dict[str, str]:
    """Copy of the process environment without Altron's Anthropic overrides."""
    blocked = {name.upper() for name in _STRIPPED_ENV_VARS}
    return {k: v for k, v in os.environ.items() if k.upper() not in blocked}


def _candidate_paths(names: tuple, extra_dirs: list) -> list:
    return [os.path.join(d, n) for d in extra_dirs if d for n in names]


def find_claude_binary() -> Optional[str]:
    """Locate the claude CLI via PATH, then common Windows install locations."""
    found = shutil.which("claude")
    if found:
        return found
    home = str(Path.home())
    dirs = [
        os.path.join(os.environ.get("APPDATA", ""), "npm") if os.environ.get("APPDATA") else "",
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".claude", "local"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "claude")
        if os.environ.get("LOCALAPPDATA") else "",
    ]
    for path in _candidate_paths(("claude.cmd", "claude.exe", "claude"), dirs):
        if os.path.isfile(path):
            return path
    return None


def find_vscode_binary() -> Optional[str]:
    return shutil.which("code") or shutil.which("code.cmd")


def resolve_project_path(project_path: Optional[str]) -> Path:
    """Default to cwd; a bare folder name lives under ~/Desktop."""
    if not project_path or not str(project_path).strip():
        return Path.cwd()
    raw = str(project_path).strip().strip('"')
    expanded = Path(os.path.expanduser(raw))
    if expanded.is_absolute():
        return expanded
    if any(sep in raw for sep in ("/", "\\")):
        return (Path.cwd() / expanded).resolve()
    return Path.home() / "Desktop" / raw


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + _TRUNCATION_NOTE


def _result(ok: bool, output: str, returncode: Optional[int], path: Optional[Path],
            opened: bool = False) -> Dict[str, Any]:
    return {
        "ok": ok,
        "output": output,
        "returncode": returncode,
        "project_path": str(path) if path is not None else None,
        "opened_in_vscode": opened,
    }


def run_claude_code(prompt: str, project_path: Optional[str] = None,
                    create_if_missing: bool = True, open_in_vscode: bool = False,
                    timeout_seconds: int = 300) -> Dict[str, Any]:
    if not str(prompt or "").strip():
        return _result(False, "A prompt is required.", None, None)
    binary = find_claude_binary()
    if not binary:
        return _result(False, "Claude Code CLI ('claude') was not found. Install it with "
                       "'npm install -g @anthropic-ai/claude-code'.", None, None)

    target = resolve_project_path(project_path)
    if not target.is_dir():
        if not create_if_missing:
            return _result(False, f"Project folder does not exist: {target}", None, target)
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as err:
            return _result(False, f"Could not create project folder: {err}", None, target)

    cmd = [binary, "-p", str(prompt), "--permission-mode", "acceptEdits",
           "--output-format", "text"]
    try:
        proc = subprocess.run(
            cmd, cwd=str(target), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_seconds,
            stdin=subprocess.DEVNULL, env=build_cli_env(),
        )
    except subprocess.TimeoutExpired:
        return _result(False, f"Claude Code timed out after {timeout_seconds}s.", None, target)
    except OSError as err:
        logger.debug("Claude Code launch failed: %s", err)
        return _result(False, f"Could not run Claude Code: {err}", None, target)

    out = (proc.stdout or "").strip()
    err_text = (proc.stderr or "").strip()
    ok = proc.returncode == 0
    if not ok:
        logger.warning("Claude Code exited with code %s; stderr: %s", proc.returncode,
                       redact_secrets(_truncate(err_text, MAX_LOGGED_STDERR_CHARS)) or "<empty>")
    combined = out if ok or not err_text else f"{out}\n{err_text}".strip()
    opened = False
    if ok and open_in_vscode:
        code = find_vscode_binary()
        if code:
            try:
                subprocess.Popen([code, str(target)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True)
                opened = True
            except OSError as err:
                logger.debug("VS Code launch failed: %s", err)
        else:
            logger.debug("VS Code binary not found")
    return _result(ok, _truncate(combined), proc.returncode, target, opened)
