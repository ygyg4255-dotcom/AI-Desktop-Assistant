"""Permission Gate for Human-in-the-Loop safety."""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from enum import Enum


class PermissionLevel(Enum):
    """Permission levels for tools."""
    ASK = "ask"           # Always ask user
    ALLOW = "allow"       # Auto-allow for this session
    DENY = "deny"         # Auto-deny


@dataclass
class PermissionRule:
    """Rule for tool permission."""
    tool_name: str
    level: PermissionLevel
    conditions: Optional[dict] = None


class PermissionGate:
    """Manages permission decisions for tool execution."""

    # Tools that are always safe (no permission needed)
    SAFE_TOOLS = {
        "file_search",
        "process_list",
        "system_info",
        "browser_open",
        "window_control",
        "web_fetch",  # SSRF rules are enforced unconditionally inside the handler
        "system_control",
        "youtube_play",
        "create_and_send_pdf",
        # The user's explicit request is the authorization for basic UI interaction and
        # for looking through the webcam; the handlers still validate their input.
        "gui_click",
        "gui_scroll",
        "capture_camera",
        "camera_start",  # turning the camera on/off is the user's explicit request;
        "camera_stop",   # frames stay in RAM and never go to Claude unless asked
    }

    # Tools that auto-run only for low-risk inputs; the handler decides per call
    # (see is_sensitive_path / needs_approval_* below) and asks otherwise.
    # They must NOT be in SAFE_TOOLS, which would pass approved=True unconditionally,
    # and a blanket "allow" (session or remembered) is refused for them.
    CONDITIONAL_TOOLS = {
        "file_read",
        "file_open",
        "app_open",
        "send_to_telegram",
    }

    # Tools that require explicit permission
    DANGEROUS_TOOLS = {
        "file_write",
        "file_delete",
        "file_move",
        "file_copy",
        "terminal_execute",
        "app_close",
        "code_edit",
        # GUI automation that types: every call is approved individually (see GUI_TOOLS)
        "gui_type",
        "gui_hotkey",
    }

    # Keyboard automation. Always DANGEROUS, and a blanket "allow" (session or
    # remembered) is refused so each keystroke batch needs its own approval.
    # (gui_click / gui_scroll are SAFE: see SAFE_TOOLS.)
    GUI_TOOLS = {"gui_type", "gui_hotkey"}

    def __init__(self):
        self._session_rules: dict[str, PermissionLevel] = {}

    def is_safe(self, tool_name: str) -> bool:
        """Check if a tool is considered safe."""
        return tool_name in self.SAFE_TOOLS

    def is_dangerous(self, tool_name: str) -> bool:
        """Check if a tool is considered dangerous."""
        return tool_name in self.DANGEROUS_TOOLS

    def requires_permission(self, tool_name: str) -> bool:
        """Check if a tool requires user permission."""
        return tool_name in self.DANGEROUS_TOOLS

    def get_permission_level(self, tool_name: str) -> PermissionLevel:
        """Get the current permission level for a tool."""
        # Check session rules first
        if tool_name in self._session_rules:
            return self._session_rules[tool_name]

        # Default based on tool type
        if self.is_safe(tool_name):
            return PermissionLevel.ALLOW
        return PermissionLevel.ASK

    def is_conditional(self, tool_name: str) -> bool:
        """True if the handler decides per call whether approval is needed."""
        return tool_name in self.CONDITIONAL_TOOLS

    def refuses_blanket_allow(self, tool_name: str) -> bool:
        """True if a session/remembered "allow always" must never apply to this tool."""
        return self.is_conditional(tool_name) or tool_name in self.GUI_TOOLS

    def set_session_permission(self, tool_name: str, level: PermissionLevel) -> None:
        """Set permission level for the current session."""
        if level == PermissionLevel.ALLOW and self.refuses_blanket_allow(tool_name):
            return  # a blanket allow would defeat the per-call checks
        self._session_rules[tool_name] = level

    def clear_session_permissions(self) -> None:
        """Clear all session permissions."""
        self._session_rules.clear()

    def get_permission_message(self, tool_name: str, tool_input: dict) -> str:
        """Generate a human-readable permission message."""
        messages = {
            "file_write": f"Write to file: {tool_input.get('path', 'unknown')}",
            "file_delete": f"Delete file: {tool_input.get('path', 'unknown')}",
            "file_copy": f"Copy: {tool_input.get('source', 'unknown')} -> {tool_input.get('destination', 'unknown')}",
            "file_move": f"Move/rename: {tool_input.get('source', 'unknown')} -> {tool_input.get('destination', 'unknown')}",
            "terminal_execute": f"Run command: {tool_input.get('command', 'unknown')[:100]}",
            "app_close": f"Close application: {tool_input.get('app_name', 'unknown')}",
            "code_edit": f"Edit file: {tool_input.get('path', 'unknown')} (targeted replace)",
            "gui_type": f"Type text: {str(tool_input.get('text', ''))[:60]!r}",
            "gui_hotkey": f"Press hotkey: {tool_input.get('keys', 'unknown')}",
        }
        return messages.get(tool_name, f"Execute tool: {tool_name}")


# === Per-call input checks used by conditional tools ===

_SENSITIVE_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".ppk", ".kdbx")
_SENSITIVE_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
_SENSITIVE_NAMES = {"memory.json", "status.json"}
_SENSITIVE_DIRS = {".ssh", ".aws", ".gnupg", "user data"}  # "User Data": Chrome/Edge/Brave profiles
_SENSITIVE_DIR_PAIRS = {
    ("mozilla", "firefox"),
    ("firefox", "profiles"),
    ("microsoft", "credentials"),
    ("microsoft", "protect"),
    ("microsoft", "vault"),
    ("system32", "config"),
}


def _canonical_candidates(path: str) -> set:
    """Resolved forms of ``path`` (symlinks and ".." resolved, case-folded)."""
    raw = os.path.expanduser(str(path).strip().strip('"'))
    return {os.path.realpath(raw).lower(), os.path.realpath(os.path.expandvars(raw)).lower()}


def is_sensitive_path(path: str) -> bool:
    """True if ``path`` resolves to a credential/secret/browser-data location."""
    if not str(path).strip():
        return False
    for resolved in _canonical_candidates(path):
        parts = list(Path(resolved).parts)
        # Drop NTFS alternate-data-stream suffixes and trailing dots/spaces Windows ignores.
        parts = [parts[0]] + [p.split(":")[0].rstrip(". ") for p in parts[1:]]
        if len(parts) < 2:
            continue
        name = parts[-1]
        if (name == ".env" or name.startswith(".env.") or name.endswith(".env")
                or name.endswith(_SENSITIVE_SUFFIXES)
                or name.startswith(_SENSITIVE_PREFIXES)
                or name in _SENSITIVE_NAMES):
            return True
        if any(p in _SENSITIVE_DIRS for p in parts[1:]):
            return True
        if any(pair in _SENSITIVE_DIR_PAIRS for pair in zip(parts[1:], parts[2:])):
            return True
    return False


RISKY_OPEN_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi", ".lnk",
    # same class of launchable/scriptable targets
    ".com", ".scr", ".pif", ".vbe", ".jse", ".wsf", ".wsh", ".hta", ".reg",
    ".msc", ".cpl", ".jar", ".msp", ".psm1", ".url", ".appref-ms", ".py", ".pyw",
}


def _has_risky_extension(path: str) -> bool:
    for candidate in {str(path).strip().strip('"'), *_canonical_candidates(path)}:
        name = Path(candidate).name.split(":")[0].rstrip(". ").lower()  # strip ADS / trailing dots
        if os.path.splitext(name)[1] in RISKY_OPEN_EXTENSIONS:
            return True
    return False


def needs_approval_file_read(path: str) -> bool:
    return is_sensitive_path(path)


def needs_approval_file_open(path: str) -> bool:
    """Documents auto-open; launchable/scriptable file types need approval."""
    return _has_risky_extension(path)


_PLAIN_APP_NAME = re.compile(r"^\w[\w .+\-()]{0,63}$")
_SETTINGS_URI = re.compile(r"^ms-settings:[\w\-./]*$", re.IGNORECASE)


def needs_approval_app_open(app_name: str = "", path: str = "") -> bool:
    """Only a plain app name (no path, extension, or shell metacharacters) auto-opens."""
    if str(path or "").strip():
        return True
    name = str(app_name or "").strip()
    if _SETTINGS_URI.match(name):
        return False
    if not _PLAIN_APP_NAME.match(name):
        return True  # separators, drive letters, quotes, &|<>^% etc.
    return _has_risky_extension(name)
