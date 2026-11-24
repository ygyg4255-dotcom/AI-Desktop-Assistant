"""Silent task scheduler: reminders and deferred tool actions persisted in ``scheduler.db``.

Firing never speaks: ``on_notify`` (HUD toast + optional Telegram, wired in ``main.py``) is the
only output channel for reminders. Actions run through ``run_action`` (the tool layer); the
permission gate is applied when the task is *scheduled* (see ``handle_schedule_task``), and the
approval result is stored in the payload as ``approved``. A task is claimed (status flipped)
before it runs, so it fires at most once even if the callback crashes.
"""
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from src.config.log import get_logger, redact_secrets
from src.config.paths import runtime_file

logger = get_logger(__name__)

KINDS = ("reminder", "action")
MAX_PENDING = 50
MAX_TITLE = 200
TICK_SECONDS = 5.0
# An action this late (e.g. Altron was off) is NOT run: a shutdown firing hours late is a surprise.
ACTION_GRACE_SECONDS = 120.0
LATE_NOTICE_SECONDS = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK(kind IN ('reminder','action')),
  title TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  run_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','done','cancelled','failed')),
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS scheduled_due ON scheduled_tasks(status, run_at);
"""


class SchedulerError(ValueError):
    """The task cannot be stored or found."""


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _fmt_remaining(secs: float) -> str:
    secs = max(0, int(secs))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else ""]
    return "".join(parts) or f"{s}s"


class SchedulerService:
    def __init__(self, path=None,
                 on_notify: Optional[Callable[[str, str], None]] = None,
                 run_action: Optional[Callable[[str, Dict[str, Any], bool], Any]] = None,
                 clock: Callable[[], float] = time.time):
        self._path = str(path if path is not None else runtime_file("scheduler.db"))
        self._on_notify = on_notify
        self._run_action = run_action
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._path, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def set_callbacks(self, on_notify=None, run_action=None) -> None:
        self._on_notify, self._run_action = on_notify, run_action

    # -- storage ---------------------------------------------------------
    def add(self, kind: str, title: str, run_at: float, payload: Optional[Dict[str, Any]] = None) -> int:
        title = redact_secrets(" ".join((title or "").split()))[:MAX_TITLE]
        if kind not in KINDS or not title:
            raise SchedulerError("A task needs a title and kind 'reminder' or 'action'.")
        with self._lock, self._conn() as c:
            if c.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE status='pending'").fetchone()[0] >= MAX_PENDING:
                raise SchedulerError(f"Too many pending tasks (max {MAX_PENDING}). Cancel one first.")
            cur = c.execute(
                "INSERT INTO scheduled_tasks(kind,title,payload_json,run_at,created_at) VALUES(?,?,?,?,?)",
                (kind, title, json.dumps(payload or {}, ensure_ascii=False), _iso(run_at), _iso(self._clock())))
            return int(cur.lastrowid)

    def list_pending(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        now = self._clock()
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM scheduled_tasks WHERE status='pending' ORDER BY run_at, id"
                             + (" LIMIT ?" if limit else ""), (limit,) if limit else ()).fetchall()
        out = []
        for r in rows:
            due = _epoch(r["run_at"])
            out.append({"id": r["id"], "kind": r["kind"], "title": r["title"], "run_at": r["run_at"],
                        "remaining": _fmt_remaining(due - now), "in_seconds": max(0, int(due - now)),
                        "action_name": json.loads(r["payload_json"]).get("action_name")})
        return out

    def cancel(self, task_id: int) -> bool:
        with self._lock, self._conn() as c:
            return c.execute("UPDATE scheduled_tasks SET status='cancelled' WHERE id=? AND status='pending'",
                             (int(task_id),)).rowcount == 1

    def cancel_matching(self, id_or_query: str) -> List[Dict[str, Any]]:
        """Cancel by id, or by keyword when it matches exactly one pending task. Returns what was cancelled."""
        key = str(id_or_query or "").strip().lstrip("#")
        pending = self.list_pending()
        if key.isdigit():
            hits = [t for t in pending if t["id"] == int(key)]
        else:
            words = [w for w in re.findall(r"\w+", key.lower()) if len(w) >= 3]
            score = {t["id"]: sum(w in f"{t['title']} {t['action_name'] or ''}".lower() for w in words) for t in pending}
            best = max(score.values(), default=0)
            hits = [t for t in pending if best and score[t["id"]] == best]  # best-matching words win ("shutdown timer")
        if not hits:
            raise SchedulerError(f"No pending task matches {key!r}.")
        if len(hits) > 1:
            ids = ", ".join(f"#{t['id']} {t['title']}" for t in hits)
            raise SchedulerError(f"Several tasks match ({ids}); cancel one by id.")
        self.cancel(hits[0]["id"])
        return hits

    # -- firing ----------------------------------------------------------
    def _finish(self, task_id: int, status: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE scheduled_tasks SET status=? WHERE id=?", (status, task_id))

    def _notify(self, title: str, text: str) -> None:
        if self._on_notify is None:
            return
        try:
            self._on_notify(title, text)
        except Exception:
            logger.exception("Scheduler notification failed")

    def fire_due(self) -> int:
        """Fire every due pending task; returns how many were handled."""
        now = self._clock()
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM scheduled_tasks WHERE status='pending' AND run_at<=? ORDER BY run_at, id",
                             (_iso(now),)).fetchall()
            # Claim before running: a crash in the callback never re-fires the task.
            claimed = [r for r in rows if c.execute(
                "UPDATE scheduled_tasks SET status='done' WHERE id=? AND status='pending'", (r["id"],)).rowcount == 1]
        for r in claimed:
            late = now - _epoch(r["run_at"])
            payload = json.loads(r["payload_json"])
            if r["kind"] == "reminder":
                prefix = f"Reminder (missed at {r['run_at'][11:16]} UTC)" if late > LATE_NOTICE_SECONDS else "Reminder"
                self._notify("Altron reminder", f"{prefix}: {r['title']}")
            elif late > ACTION_GRACE_SECONDS:
                self._finish(r["id"], "failed")
                self._notify("Altron scheduled action", f"Skipped (missed while Altron was off): {r['title']}")
            else:
                self._run_one_action(r["id"], r["title"], payload)
        return len(claimed)

    def _run_one_action(self, task_id: int, title: str, payload: Dict[str, Any]) -> None:
        try:
            if self._run_action is None:
                raise RuntimeError("no action runner configured")
            res = self._run_action(str(payload.get("action_name", "")), dict(payload.get("action_args") or {}),
                                   bool(payload.get("approved")))
            ok = bool(getattr(res, "success", False))
            detail = getattr(res, "error", None) or "done"
            out = getattr(res, "output", None)
            if ok and isinstance(out, dict) and out.get("message"):
                detail = out["message"]
        except Exception as err:
            logger.exception("Scheduled action %s failed", task_id)
            ok, detail = False, str(err) or type(err).__name__
        if not ok:
            self._finish(task_id, "failed")
        self._notify("Altron scheduled action", f"{'Done' if ok else 'Failed'}: {title} - {detail}")

    # -- thread ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="AltronScheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():  # first pass runs immediately: tasks missed while off are handled at boot
            try:
                self.fire_due()
            except Exception:
                logger.exception("Scheduler loop error")
            self._stop.wait(TICK_SECONDS)


def format_block(tasks: List[Dict[str, Any]], limit: int = 5) -> str:
    """Compact reference block for the user turn (never the system prompt: keeps prompt caching intact)."""
    if not tasks:
        return ""
    lines = "\n".join(f"- #{t['id']} {t['kind']}: {t['title']} (in {t['remaining']})" for t in tasks[:limit])
    return f"[Scheduled tasks - reference only]\n{lines}\n[End scheduled tasks]\n\n"


_service: Optional[SchedulerService] = None
_service_lock = threading.Lock()


def get_scheduler() -> SchedulerService:
    global _service
    with _service_lock:
        if _service is None:
            _service = SchedulerService()
        return _service


def reset_scheduler() -> None:
    """Stop and drop the singleton (tests; re-reads the runtime dir on next use)."""
    global _service
    with _service_lock:
        svc, _service = _service, None
    if svc is not None:
        svc.stop()
