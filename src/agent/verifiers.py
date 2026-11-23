"""Zero-cost post-execution verifiers: check the real state after a tool reports success.

A verifier gets (tool_input, result) and returns a failure detail string, or None when the
check passes or cannot be made. ``verify_result`` never raises.
"""
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.agent.tools import APP_URI_MAP, ToolResult, resolve_user_path
from src.config.log import get_logger

logger = get_logger(__name__)


def _file_write(inp: Dict[str, Any], res: ToolResult) -> Optional[str]:
    path = resolve_user_path(inp.get("path", ""))
    return None if path.exists() else f"Verification failed: {path} does not exist after the write."


def _file_move(inp: Dict[str, Any], res: ToolResult) -> Optional[str]:
    src, dst = resolve_user_path(inp.get("source", "")), resolve_user_path(inp.get("destination", ""))
    final = dst / src.name if dst.is_dir() else dst
    if final == src:
        return None
    if not final.exists():
        return f"Verification failed: {final} does not exist after the move."
    if src.exists():
        return f"Verification failed: {src} still exists after the move."
    return None


def _file_delete(inp: Dict[str, Any], res: ToolResult) -> Optional[str]:
    path = resolve_user_path(inp.get("path", ""))
    return f"Verification failed: {path} still exists after the delete." if path.exists() else None


def _app_open(inp: Dict[str, Any], res: ToolResult) -> Optional[str]:
    # shortcut: only apps launched from a PATH executable are checked (Store/URI apps have no
    # predictable process name); upgrade to a window-title check if those matter.
    name = str(inp.get("app_name") or "").strip()
    exe = shutil.which(name) if name and name.lower() not in APP_URI_MAP else None
    if not exe:
        return None
    import psutil
    stem = Path(exe).stem.lower()
    deadline = time.time() + 2.0
    while True:
        if any(stem in (p.info["name"] or "").lower() for p in psutil.process_iter(["name"])):
            return None
        if time.time() >= deadline:
            return f"Verification failed: no running process for '{name}' after launch."
        time.sleep(0.25)


def _volume(inp: Dict[str, Any], res: ToolResult) -> Optional[str]:
    # Readback needs pycaw; without it (or on any API mismatch) the check is skipped, not failed.
    if str(inp.get("action")) != "set_volume":
        return None
    try:
        from pycaw.pycaw import AudioUtilities
        level = round(AudioUtilities.GetSpeakers().EndpointVolume.GetMasterVolumeLevelScalar() * 100)
    except Exception as err:
        logger.debug("volume readback unavailable: %s", err)
        return None
    want = int(res.output.get("volume", -1)) if isinstance(res.output, dict) else -1
    return None if abs(level - want) <= 4 else f"Verification failed: volume reads {level}%, expected {want}%."


VERIFIERS: Dict[str, Callable[[Dict[str, Any], ToolResult], Optional[str]]] = {
    "file_write": _file_write,
    "file_move": _file_move,
    "file_delete": _file_delete,
    "app_open": _app_open,
    "system_control": _volume,
}


def verify_result(tool_name: str, tool_input: Dict[str, Any], result: ToolResult) -> Optional[str]:
    check = VERIFIERS.get(tool_name)
    if check is None:
        return None
    try:
        return check(tool_input, result)
    except Exception as err:
        logger.debug("verifier for %s errored: %s", tool_name, err)
        return None
