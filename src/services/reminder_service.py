"""Native reminders / alarms: a small thread-safe scheduler persisted to ``reminders.json``.

Reminders live in the runtime dir (``runtime_file``) so they survive restarts. A reminder
that came due while Altron was off fires once at the next start, marked as late. The
service only decides *when* to fire; what firing does (speak, HUD, Telegram) is the
``on_fire`` callback wired in ``main.py``. Each reminder fires at most once: it is removed
and persisted *before* the callback runs.
"""
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from src.config.log import get_logger, redact_secrets
from src.config.paths import runtime_file

logger = get_logger(__name__)

REMINDERS_FILENAME = "reminders.json"
MAX_MESSAGE_CHARS = 500
MAX_PENDING = 50
MAX_DELAY_SECONDS = 366 * 24 * 3600
LATE_GRACE_SECONDS = 60.0   # fired this long after its due time => spoken as "missed"
_MAX_WAIT = 30.0            # the worker re-checks at least this often (clock changes, sleep/resume)

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DELAY_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)(?![a-z])")
_CLOCK = re.compile(r"^(\d{1,2})\s*[:.]\s*(\d{2})(?:\s*[:.]\s*(\d{2}))?\s*(am|pm)?$")
_CLOCK_AMPM = re.compile(r"^(\d{1,2})\s*(am|pm)$")


class ReminderError(ValueError):
    """The time/message could not be understood or the reminder cannot be stored."""


@dataclass
class Reminder:
    id: str
    message: str
    due: float       # epoch seconds
    created: float

    def due_text(self) -> str:
        return datetime.fromtimestamp(self.due).strftime("%Y-%m-%d %H:%M")

    def public_view(self, now: Optional[float] = None) -> Dict[str, object]:
        now = time.time() if now is None else now
        return {
            "id": self.id,
            "message": self.message,
            "due": self.due_text(),
            "in_seconds": max(0, int(self.due - now)),
        }


def _resolve_clock(hour: int, minute: int, second: int, now: datetime) -> float:
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ReminderError("That is not a valid time of day.")
    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if target <= now:
        target += timedelta(days=1)  # already passed today: next occurrence is tomorrow
    return target.timestamp()


def parse_when(text: Optional[str] = None, delay_seconds: Optional[float] = None,
               now: Optional[float] = None) -> float:
    """Epoch due time from a clock time ("22:40", "7:30 pm"), an ISO datetime, a relative
    delay ("in 10 minutes", "1h30m") or ``delay_seconds``. A clock time that already
    passed today rolls to tomorrow. Raises ReminderError otherwise."""
    now = time.time() if now is None else now
    if delay_seconds not in (None, ""):
        try:
            secs = float(delay_seconds)
        except (TypeError, ValueError):
            raise ReminderError("delay_seconds must be a number.") from None
        if not 0 < secs <= MAX_DELAY_SECONDS:
            raise ReminderError("The delay must be positive and under a year.")
        return now + secs

    raw = (text or "").strip().lower()
    if not raw:
        raise ReminderError("Say when: a time like 22:40, or a delay like 'in 10 minutes'.")
    raw = re.sub(r"^(?:at|on|in|after)\s+", "", raw)

    # Relative delay: every character must belong to a "<number> <unit>" group.
    parts = list(_DELAY_PART.finditer(raw))
    if parts and re.sub(r"[\s,]|and", "", _DELAY_PART.sub("", raw)) == "":
        secs = sum(float(m.group(1)) * _UNITS[m.group(2)[0]] for m in parts)
        if not 0 < secs <= MAX_DELAY_SECONDS:
            raise ReminderError("The delay must be positive and under a year.")
        return now + secs

    local_now = datetime.fromtimestamp(now)
    match = _CLOCK.match(raw)
    if match:
        hour, minute, second = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
        meridiem = match.group(4)
        if meridiem:
            if not 1 <= hour <= 12:
                raise ReminderError("That is not a valid time of day.")
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
        return _resolve_clock(hour, minute, second, local_now)
    match = _CLOCK_AMPM.match(raw)
    if match:
        hour = int(match.group(1))
        if not 1 <= hour <= 12:
            raise ReminderError("That is not a valid time of day.")
        return _resolve_clock(hour % 12 + (12 if match.group(2) == "pm" else 0), 0, 0, local_now)

    try:
        when = datetime.fromisoformat((text or "").strip())
    except ValueError:
        raise ReminderError(
            f"I could not understand the time {text!r}. Use e.g. '22:40', 'in 10 minutes' or an ISO datetime."
        ) from None
    due = when.timestamp()  # a naive datetime is local time
    if due <= now:
        raise ReminderError("That date and time is already in the past.")
    if due - now > MAX_DELAY_SECONDS:
        raise ReminderError("That is more than a year away.")
    return due


class ReminderService:
    """Thread-safe reminder store with a background firing thread."""

    def __init__(self, path: Optional[Union[str, Path]] = None,
                 on_fire: Optional[Callable[[Reminder], None]] = None,
                 clock: Callable[[], float] = time.time):
        self._path = Path(path) if path is not None else runtime_file(REMINDERS_FILENAME)
        self._on_fire = on_fire
        self._clock = clock
        self._cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self._reminders: Dict[str, Reminder] = {}
        self._load()

    # -- wiring ---------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    def set_callback(self, on_fire: Optional[Callable[[Reminder], None]]) -> None:
        self._on_fire = on_fire

    # -- persistence ----------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            logger.warning("Ignoring unreadable reminders file (%s): %s", self._path.name, err)
            return
        for item in data if isinstance(data, list) else []:
            try:
                rem = Reminder(str(item["id"]), str(item["message"]), float(item["due"]), float(item["created"]))
            except (KeyError, TypeError, ValueError):
                logger.debug("Skipping malformed reminder entry: %r", item)
                continue
            self._reminders[rem.id] = rem

    def _save_locked(self) -> None:
        payload = [asdict(r) for r in sorted(self._reminders.values(), key=lambda r: r.due)]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as err:
            logger.warning("Could not save reminders: %s", err)

    # -- public API -----------------------------------------------------
    def add(self, message: str, due: float) -> Reminder:
        text = redact_secrets(" ".join((message or "").split()))[:MAX_MESSAGE_CHARS]
        if not text:
            raise ReminderError("A reminder needs a message.")
        with self._cond:
            if len(self._reminders) >= MAX_PENDING:
                raise ReminderError(f"Too many pending reminders (max {MAX_PENDING}). Cancel one first.")
            rem = Reminder(secrets.token_hex(3), text, float(due), self._clock())
            self._reminders[rem.id] = rem
            self._save_locked()
            self._cond.notify_all()
        logger.info("Reminder %s set for %s", rem.id, rem.due_text())
        return rem

    def list(self) -> List[Reminder]:
        with self._cond:
            return sorted(self._reminders.values(), key=lambda r: r.due)

    def cancel(self, reminder_id: str) -> bool:
        key = str(reminder_id or "").strip().lstrip("#")
        with self._cond:
            if self._reminders.pop(key, None) is None:
                return False
            self._save_locked()
            self._cond.notify_all()
        logger.info("Reminder %s cancelled", key)
        return True

    def fire_due(self) -> List[Reminder]:
        """Fire every reminder whose time has come (oldest first) and return them."""
        with self._cond:
            now = self._clock()
            due = sorted((r for r in self._reminders.values() if r.due <= now), key=lambda r: r.due)
            for rem in due:
                del self._reminders[rem.id]
            if due:
                self._save_locked()  # persist first: a crash in the callback never re-fires
        callback = self._on_fire
        for rem in due:
            logger.info("Reminder %s fired", rem.id)
            if callback is None:
                continue
            try:
                callback(rem)
            except Exception:
                logger.exception("Reminder callback failed for %s", rem.id)
        return due

    # -- background thread ---------------------------------------------
    def start(self) -> None:
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._loop, name="AltronReminders", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _loop(self) -> None:
        while True:
            try:
                self.fire_due()
            except Exception:
                logger.exception("Reminder loop error")
            with self._cond:
                if self._stopping:
                    return
                nxt = min((r.due for r in self._reminders.values()), default=None)
                wait = _MAX_WAIT if nxt is None else min(_MAX_WAIT, max(0.0, nxt - self._clock()))
                if wait > 0:
                    self._cond.wait(timeout=wait)
                if self._stopping:
                    return


def format_reminder_speech(rem: Reminder, now: Optional[float] = None) -> str:
    """Text spoken/logged when a reminder fires; flags ones missed while Altron was off."""
    now = time.time() if now is None else now
    if now - rem.due > LATE_GRACE_SECONDS:
        when = datetime.fromtimestamp(rem.due).strftime("%H:%M")
        return f"Reminder (missed at {when}): {rem.message}"
    return f"Reminder: {rem.message}"


_service: Optional[ReminderService] = None
_service_lock = threading.Lock()


def get_reminder_service() -> ReminderService:
    """Process-wide service shared by the agent tools and main.py."""
    global _service
    with _service_lock:
        if _service is None:
            _service = ReminderService()
        return _service


def reset_reminder_service() -> None:
    """Stop and drop the singleton (tests; re-reads the runtime dir on next use)."""
    global _service
    with _service_lock:
        svc, _service = _service, None
    if svc is not None:
        svc.stop()
