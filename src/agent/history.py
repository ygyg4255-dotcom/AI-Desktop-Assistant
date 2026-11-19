"""Soft, reference-only persistent conversation history.

Recent user/assistant *text* turns are kept in ``conversation_history.json`` (runtime
dir) so Altron has continuity across restarts. Everything is passed through
``redact_secrets`` before it is written, the file is capped, and on startup only a
compact, truncated slice is exposed, labelled as background reference. It is never
replayed as live conversation or tool state.
"""
import json
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Union

from src.config.log import get_logger, redact_secrets
from src.config.paths import runtime_file
from src.agent.vision import scrub_image_data

logger = get_logger(__name__)

HISTORY_FILENAME = "conversation_history.json"
MAX_STORED_TURNS = 30       # file cap (each user or assistant message is one turn)
MAX_STORED_CHARS = 1500     # per-turn cap on disk
REFERENCE_TURNS = 8         # how many recent turns are exposed at startup
REFERENCE_CHARS = 300       # truncation per exposed turn
REFERENCE_HEADER = "[Previous session context — for reference only]"


class PersistentHistory:
    def __init__(self, path: Optional[Union[str, Path]] = None, max_turns: int = MAX_STORED_TURNS):
        self._path = Path(path) if path is not None else runtime_file(HISTORY_FILENAME)
        self._max_turns = max_turns
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> List[Dict[str, str]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as err:
            logger.warning("Ignoring unreadable conversation history (%s): %s", self._path.name, err)
            return []
        if not isinstance(data, list):
            return []
        return [t for t in data if isinstance(t, dict) and t.get("role") in ("user", "assistant")
                and isinstance(t.get("content"), str)]

    def _write(self, turns: List[Dict[str, str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(turns, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def append(self, role: str, content: str) -> None:
        """Store one turn (redacted, length-capped). Never raises: history is best-effort."""
        if role not in ("user", "assistant") or not content or not content.strip():
            return
        clean = redact_secrets(scrub_image_data(content.strip()))[:MAX_STORED_CHARS]
        with self._lock:
            try:
                turns = self._read()
                turns.append({"role": role, "content": clean})
                self._write(turns[-self._max_turns:])
            except OSError as err:
                logger.warning("Could not save conversation history: %s", err)

    def turns(self) -> List[Dict[str, str]]:
        with self._lock:
            return self._read()

    def load_reference(self, max_turns: int = REFERENCE_TURNS, max_chars: int = REFERENCE_CHARS) -> str:
        """Compact labelled block of the latest turns, or "" when there is nothing saved."""
        recent = self.turns()[-max_turns:]
        if not recent:
            return ""
        lines = [REFERENCE_HEADER]
        for turn in recent:
            text = " ".join(turn["content"].split())
            if len(text) > max_chars:
                text = text[:max_chars].rstrip() + "…"
            who = "User" if turn["role"] == "user" else "Altron"
            lines.append(f"- {who}: {text}")
        return "\n".join(lines)

    def clear(self) -> None:
        with self._lock:
            try:
                self._write([])
            except OSError as err:
                logger.warning("Could not clear conversation history: %s", err)
