"""Daily to-do list and quick notes, persisted as JSON in the runtime dir.

Everything is stored in ``notes.json`` under ``runtime_file`` (audit S8), so it survives
restarts and never lands in the OneDrive-synced project folder. Writes are atomic
(temp file + ``os.replace``) and serialised by a lock. A corrupt file is moved aside
rather than overwritten, so no data is silently discarded.
"""
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Union

from src.config.log import get_logger
from src.config.paths import runtime_file

logger = get_logger(__name__)

NOTES_FILENAME = "notes.json"
MAX_TASK_CHARS = 300
MAX_DUE_CHARS = 100
MAX_TITLE_CHARS = 120
MAX_NOTE_CHARS = 20000
MAX_TODOS = 500
MAX_NOTES = 200


class NotesError(ValueError):
    """The request is invalid or refers to something that does not exist."""


def _new_id(existing: Dict[str, object]) -> str:
    while True:
        candidate = secrets.token_hex(3)
        if candidate not in existing:
            return candidate


def _clean(value: object, field: str, limit: int, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise NotesError(f"{field} is required.")
    if len(text) > limit:
        raise NotesError(f"{field} must be at most {limit} characters.")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        raise NotesError(f"{field} contains control characters.")
    return text


class NotesService:
    """Thread-safe to-do list and note store."""

    def __init__(self, path: Optional[Union[str, Path]] = None):
        self._path = Path(path) if path is not None else runtime_file(NOTES_FILENAME)
        self._lock = threading.RLock()
        self._todos: Dict[str, dict] = {}
        self._notes: Dict[str, dict] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top-level JSON value is not an object")
            self._todos = {str(t["id"]): t for t in data.get("todos", [])}
            self._notes = {str(n["id"]): n for n in data.get("notes", [])}
        except (OSError, ValueError, KeyError, TypeError) as err:
            aside = self._path.with_name(f"{self._path.name}.corrupt-{int(time.time())}")
            logger.warning("notes file unreadable (%s); moved aside to %s", err, aside.name)
            try:
                os.replace(self._path, aside)
            except OSError as move_err:
                logger.error("could not move corrupt notes file aside: %s", move_err)
            self._todos, self._notes = {}, {}

    def _save_locked(self) -> None:
        payload = {"todos": list(self._todos.values()), "notes": list(self._notes.values())}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    # -- to-do list --------------------------------------------------------
    def todo_add(self, task: str, due: Optional[str] = None) -> dict:
        task = _clean(task, "task", MAX_TASK_CHARS)
        due_text = _clean(due, "due", MAX_DUE_CHARS, required=False) if due else ""
        with self._lock:
            if len(self._todos) >= MAX_TODOS:
                raise NotesError(f"The to-do list is full ({MAX_TODOS} items).")
            item = {
                "id": _new_id(self._todos), "task": task, "due": due_text,
                "done": False, "created": time.time(), "completed": None,
            }
            self._todos[item["id"]] = item
            self._save_locked()
            return dict(item)

    def todo_list(self, include_done: bool = False) -> List[dict]:
        with self._lock:
            items = [dict(t) for t in self._todos.values() if include_done or not t["done"]]
        return sorted(items, key=lambda t: (t["done"], t["created"]))

    def todo_complete(self, task_id: str) -> dict:
        with self._lock:
            item = self._get_todo(task_id)
            if not item["done"]:
                item["done"] = True
                item["completed"] = time.time()
                self._save_locked()
            return dict(item)

    def todo_delete(self, task_id: str) -> dict:
        with self._lock:
            item = self._get_todo(task_id)
            del self._todos[item["id"]]
            self._save_locked()
            return dict(item)

    def _get_todo(self, task_id: str) -> dict:
        key = str(task_id or "").strip()
        item = self._todos.get(key)
        if item is None:
            raise NotesError(f"No to-do item with id {key!r}.")
        return item

    # -- quick notes -------------------------------------------------------
    def note_save(self, title: str, content: str) -> dict:
        """Create a note, or replace the content of the existing note with the same title."""
        title = _clean(title, "title", MAX_TITLE_CHARS)
        content = _clean(content, "content", MAX_NOTE_CHARS, required=False)
        with self._lock:
            existing = self._find_note_by_title(title)
            now = time.time()
            if existing is not None:
                existing["content"] = content
                existing["updated"] = now
                self._save_locked()
                return dict(existing)
            if len(self._notes) >= MAX_NOTES:
                raise NotesError(f"Notes are full ({MAX_NOTES} notes). Delete or rename one first.")
            note = {"id": _new_id(self._notes), "title": title, "content": content,
                    "created": now, "updated": now}
            self._notes[note["id"]] = note
            self._save_locked()
            return dict(note)

    def note_list(self) -> List[dict]:
        with self._lock:
            notes = [{"id": n["id"], "title": n["title"], "updated": n["updated"]}
                     for n in self._notes.values()]
        return sorted(notes, key=lambda n: n["updated"], reverse=True)

    def note_read(self, title_or_id: str) -> dict:
        key = str(title_or_id or "").strip()
        if not key:
            raise NotesError("A note title or id is required.")
        with self._lock:
            if key in self._notes:
                return dict(self._notes[key])
            by_title = self._find_note_by_title(key)
            if by_title is not None:
                return dict(by_title)
            folded = key.casefold()
            partial = [n for n in self._notes.values() if folded in n["title"].casefold()]
        if len(partial) == 1:
            return dict(partial[0])
        if len(partial) > 1:
            titles = ", ".join(repr(n["title"]) for n in partial[:5])
            raise NotesError(f"Several notes match {key!r}: {titles}. Use the exact title.")
        raise NotesError(f"No note called {key!r}.")

    def _find_note_by_title(self, title: str) -> Optional[dict]:
        folded = title.casefold()
        for note in self._notes.values():
            if note["title"].casefold() == folded:
                return note
        return None


_service: Optional[NotesService] = None
_service_lock = threading.Lock()


def get_notes_service() -> NotesService:
    """Process-wide notes store shared by the agent tools."""
    global _service
    with _service_lock:
        if _service is None:
            _service = NotesService()
        return _service


def reset_notes_service() -> None:
    """Drop the singleton (tests; re-reads the runtime dir on next use)."""
    global _service
    with _service_lock:
        _service = None
