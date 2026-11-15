"""Permission Gate for Human-in-the-Loop safety."""
from dataclasses import dataclass
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
        "file_read",
        "file_copy",
        "file_open",
        "process_list",
        "system_info",
        "browser_open",
        "app_open",
        "window_control",
        "web_fetch",
        "system_control",
        "youtube_play",
        "create_and_send_pdf",
        "send_to_telegram"
    }

    # Tools that require explicit permission
    DANGEROUS_TOOLS = {
        "file_write",
        "file_delete",
        "file_move",
        "terminal_execute",
        "app_close",
        "code_edit"
    }

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

    def set_session_permission(self, tool_name: str, level: PermissionLevel) -> None:
        """Set permission level for the current session."""
        self._session_rules[tool_name] = level

    def clear_session_permissions(self) -> None:
        """Clear all session permissions."""
        self._session_rules.clear()

    def get_permission_message(self, tool_name: str, tool_input: dict) -> str:
        """Generate a human-readable permission message."""
        messages = {
            "file_write": f"Write to file: {tool_input.get('path', 'unknown')}",
            "file_delete": f"Delete file: {tool_input.get('path', 'unknown')}",
            "file_move": f"Move/rename: {tool_input.get('source', 'unknown')} -> {tool_input.get('destination', 'unknown')}",
            "terminal_execute": f"Run command: {tool_input.get('command', 'unknown')[:100]}",
            "app_close": f"Close application: {tool_input.get('app_name', 'unknown')}",
            "code_edit": f"Edit file: {tool_input.get('path', 'unknown')} (targeted replace)"
        }
        return messages.get(tool_name, f"Execute tool: {tool_name}")
