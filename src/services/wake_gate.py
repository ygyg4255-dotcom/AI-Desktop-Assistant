"""Wake-word gate for the voice pipeline (Qt-free, no I/O, unit-testable).

Utterances are ignored unless they start with the wake word ("altron" and common
STT variants). Two things open the gate without a wake word:

* ``arm_direct()`` (the global Alt+R hotkey): the next utterance is accepted as-is.
* a bare wake word ("Altron"): a short active-listening window (``ACTIVE_WINDOW_S``)
  in which the next utterance is accepted as-is.

Both are single-use and expire, so stray speech later is ignored again.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

ACTIVE_WINDOW_S = 6.0   # listening window after a bare wake word
DIRECT_WINDOW_S = 30.0  # how long an Alt+R arm stays valid if nothing is said

_VARIANTS = (
    r"altron|ultron|altran|ultran|eltron|oltron|al\s+tron|el\s+tron|ul\s+tron|"
    r"الترون|ألترون|إلترون|آلترون|أولترون|اولترون"
)
# Wake word must open the utterance (optionally after a greeting/filler).
_LEADING_WAKE = re.compile(
    rf"^[\s,.!?،؟\"'-]*(?:(?:hey|hi|hello|ok|okay|yo)[\s,]+)?(?:{_VARIANTS})(?![\w؀-ۿ])",
    re.IGNORECASE,
)
_TRIM = " ,.:;!?،؟-\t\n"

ACTION_IGNORE = "ignore"
ACTION_ACK = "ack"   # wake word only: acknowledge, window opened
ACTION_RUN = "run"   # forward ``query`` to the agent


def strip_wake_word(text: str) -> tuple[bool, str]:
    """(starts_with_wake_word, command_with_wake_word_removed). Case-insensitive."""
    if not text:
        return False, ""
    match = _LEADING_WAKE.match(text)
    if not match:
        return False, ""
    return True, text[match.end():].strip(_TRIM)


@dataclass(frozen=True)
class WakeDecision:
    action: str
    query: str = ""
    bypassed: bool = False  # accepted via hotkey / active window rather than wake word


class WakeGate:
    """Decides what to do with each recognized utterance."""

    def __init__(self, require_wake_word: bool = True,
                 clock: Callable[[], float] = time.monotonic):
        self.require_wake_word = require_wake_word
        self._clock = clock
        self._lock = threading.Lock()
        self._direct_until = 0.0
        self._active_until = 0.0

    def arm_direct(self, window: float = DIRECT_WINDOW_S) -> None:
        """Alt+R: the next utterance bypasses the wake word."""
        with self._lock:
            self._direct_until = self._clock() + window

    def open_active_window(self, window: float = ACTIVE_WINDOW_S) -> None:
        with self._lock:
            self._active_until = self._clock() + window

    def _consume_bypass(self) -> bool:
        now = self._clock()
        with self._lock:
            armed = now <= self._direct_until or now <= self._active_until
            self._direct_until = self._active_until = 0.0
            return armed

    def evaluate(self, text: str) -> WakeDecision:
        text = (text or "").strip()
        if not text:
            return WakeDecision(ACTION_IGNORE)
        if not self.require_wake_word:
            has_wake, rest = strip_wake_word(text)
            if has_wake and not rest:
                return WakeDecision(ACTION_ACK)
            return WakeDecision(ACTION_RUN, rest if has_wake else text)

        has_wake, rest = strip_wake_word(text)
        if has_wake:
            with self._lock:  # an explicit wake word supersedes any pending bypass
                self._direct_until = self._active_until = 0.0
            if rest:
                return WakeDecision(ACTION_RUN, rest)
            self.open_active_window()
            return WakeDecision(ACTION_ACK)
        if self._consume_bypass():
            return WakeDecision(ACTION_RUN, text, bypassed=True)
        return WakeDecision(ACTION_IGNORE)
