"""Qt-free signal shim for the headless runtime (main.py).

``CallbackSignal`` mimics the small part of the Qt ``Signal`` API the listener
loops use (``connect`` / ``disconnect`` / ``emit``) with plain callbacks invoked
synchronously on the emitting thread, i.e. like ``Qt.DirectConnection``. The
legacy QThread wrappers shadow these attributes with real Qt signals.
"""
import logging
import threading
from typing import Any, Callable, List

logger = logging.getLogger("altron.signals")


class _BoundCallbacks:
    def __init__(self, name: str):
        self._name = name
        self._lock = threading.Lock()
        self._slots: List[Callable[..., Any]] = []

    def connect(self, slot: Callable[..., Any], *_qt_connection_type: Any) -> None:
        with self._lock:
            self._slots.append(slot)

    def disconnect(self, slot: Callable[..., Any]) -> None:
        with self._lock:
            if slot in self._slots:
                self._slots.remove(slot)

    def emit(self, *args: Any) -> None:
        with self._lock:
            slots = list(self._slots)
        for slot in slots:
            try:
                slot(*args)
            except Exception:
                # A failing slot must not kill the listener loop (Qt behaves the same).
                logger.exception("Unhandled error in %s handler", self._name)


class CallbackSignal:
    """Class-level descriptor giving each instance its own callback list."""

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name
        self._attr = f"_signal_{name}"

    def __get__(self, obj: Any, owner: type = None) -> Any:
        if obj is None:
            return self
        bound = obj.__dict__.get(self._attr)
        if bound is None:
            bound = obj.__dict__.setdefault(self._attr, _BoundCallbacks(self._name))
        return bound
