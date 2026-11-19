"""Global Windows hotkey via Win32 ``RegisterHotKey`` (ctypes, no extra dependencies).

``GlobalHotkey`` runs a message loop on its own thread: it registers the hotkey
on that thread, calls back on every press and unregisters it when stopped.
On non-Windows platforms ``start()`` is a logged no-op.
"""
from __future__ import annotations

import sys
import threading
from typing import Callable

from src.config.log import get_logger

logger = get_logger(__name__)

MOD_ALT = 0x0001
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
_HOTKEY_ID = 0xA17


class GlobalHotkey:
    def __init__(self, callback: Callable[[], None], vk: int = ord("R"),
                 modifiers: int = MOD_ALT, label: str = "Alt+R"):
        self._callback = callback
        self._vk = vk
        self._modifiers = modifiers | MOD_NOREPEAT
        self._label = label
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.registered = False

    def start(self) -> bool:
        """Start the hotkey thread; True when the hotkey was registered."""
        if sys.platform != "win32":
            logger.info("Global hotkey %s is only supported on Windows", self._label)
            return False
        self._thread = threading.Thread(target=self._loop, name="AltronHotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3)
        return self.registered

    def stop(self) -> None:
        """Unregister the hotkey and end the message loop."""
        thread = self._thread
        if thread is None:
            return
        if self._thread_id:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception as err:
                logger.debug("hotkey stop signal failed: %s", err)
        thread.join(timeout=2)
        self._thread = None

    def _loop(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        try:
            if not user32.RegisterHotKey(None, _HOTKEY_ID, self._modifiers, self._vk):
                logger.warning("Could not register global hotkey %s (already in use?)", self._label)
                return
            self.registered = True
            logger.info("Global hotkey %s registered", self._label)
            self._ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    try:
                        self._callback()
                    except Exception:
                        logger.exception("Hotkey callback failed")
        except Exception:
            logger.exception("Global hotkey loop failed")
        finally:
            if self.registered:
                user32.UnregisterHotKey(None, _HOTKEY_ID)
                self.registered = False
                logger.info("Global hotkey %s unregistered", self._label)
            self._ready.set()
