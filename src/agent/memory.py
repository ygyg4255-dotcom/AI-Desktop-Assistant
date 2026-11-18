"""Thread-safe conversation memory storage."""
import json
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional, Union

from src.config.paths import runtime_file


class ConversationMemory:
    """Thread-safe short-term conversation memory storing the last N messages."""

    def __init__(self, memory_file: Optional[Union[str, Path]] = None, max_messages: int = 10):
        if memory_file is None:
            self._file_path = runtime_file("memory.json")
        else:
            self._file_path = Path(memory_file)

        self._max_messages = max_messages
        self._lock = threading.Lock()
        self._ensure_file()

    def _ensure_file(self) -> None:
        """Ensure memory.json exists with valid JSON array."""
        with self._lock:
            if not self._file_path.exists():
                try:
                    self._file_path.parent.mkdir(parents=True, exist_ok=True)
                    self._file_path.write_text("[]", encoding="utf-8")
                except Exception:
                    pass

    def _read_messages(self) -> List[Dict[str, str]]:
        """Read messages from memory.json under lock."""
        if not self._file_path.exists():
            return []
        try:
            content = self._file_path.read_text(encoding="utf-8").strip()
            if not content:
                return []
            data = json.loads(content)
            if isinstance(data, list):
                return data
            return []
        except Exception:
            return []

    def _write_messages(self, messages: List[Dict[str, str]]) -> None:
        """Write messages to memory.json under lock."""
        try:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            trimmed = messages[-self._max_messages:]
            self._file_path.write_text(
                json.dumps(trimmed, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def add_message(self, role: str, content: str) -> None:
        """Append a message and trim to last max_messages."""
        if not content:
            return
        with self._lock:
            messages = self._read_messages()
            messages.append({"role": role, "content": content})
            self._write_messages(messages)

    def get_recent_messages(self, limit: int = 10) -> List[Dict[str, str]]:
        """Retrieve recent conversation messages up to limit."""
        with self._lock:
            messages = self._read_messages()
            return messages[-limit:]

    def format_for_prompt(self, limit: int = 10) -> str:
        """Format recent messages into a prompt-friendly transcript string."""
        msgs = self.get_recent_messages(limit=limit)
        if not msgs:
            return ""
        lines = []
        for m in msgs:
            role = "User" if m.get("role") == "user" else "Altron"
            lines.append(f"{role}: {m.get('content', '')}")
        return "\n".join(lines)

    def clear(self) -> None:
        """Clear all messages from memory.json."""
        with self._lock:
            self._write_messages([])
