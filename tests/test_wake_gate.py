import pytest

from src.services.hotkey import GlobalHotkey
from src.services.wake_gate import (
    ACTION_ACK, ACTION_IGNORE, ACTION_RUN, ACTIVE_WINDOW_S, WakeGate, strip_wake_word,
)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gate(clock):
    return WakeGate(clock=clock)


@pytest.mark.parametrize("text,expected", [
    ("Altron open WhatsApp", "open WhatsApp"),
    ("altron, open WhatsApp", "open WhatsApp"),
    ("ALTRON what time is it", "what time is it"),
    ("Ultron open WhatsApp", "open WhatsApp"),
    ("Altran open notepad", "open notepad"),
    ("hey Altron open notepad", "open notepad"),
    ("ألترون افتح واتساب", "افتح واتساب"),
    ("Al tron open notepad", "open notepad"),
])
def test_wake_word_stripped(text, expected):
    assert strip_wake_word(text) == (True, expected)


@pytest.mark.parametrize("text", [
    "open WhatsApp",
    "tell altron to open WhatsApp",  # not at the start
    "Altronics store hours",         # different word
    "",
])
def test_no_wake_word(text):
    assert strip_wake_word(text)[0] is False


def test_wake_word_with_command_runs(gate):
    d = gate.evaluate("Ultron open WhatsApp")
    assert (d.action, d.query, d.bypassed) == (ACTION_RUN, "open WhatsApp", False)


def test_speech_without_wake_word_is_ignored(gate):
    assert gate.evaluate("open WhatsApp").action == ACTION_IGNORE
    assert gate.evaluate("   ").action == ACTION_IGNORE


def test_bare_wake_word_opens_active_window(gate, clock):
    assert gate.evaluate("Altron").action == ACTION_ACK
    clock.t += ACTIVE_WINDOW_S - 1
    d = gate.evaluate("open WhatsApp")
    assert (d.action, d.query, d.bypassed) == (ACTION_RUN, "open WhatsApp", True)
    assert gate.evaluate("open notepad").action == ACTION_IGNORE  # single use


def test_active_window_expires(gate, clock):
    gate.evaluate("Altron")
    clock.t += ACTIVE_WINDOW_S + 1
    assert gate.evaluate("open WhatsApp").action == ACTION_IGNORE


def test_alt_r_bypasses_wake_word_once(gate):
    gate.arm_direct()
    d = gate.evaluate("open WhatsApp")
    assert (d.action, d.query, d.bypassed) == (ACTION_RUN, "open WhatsApp", True)
    assert gate.evaluate("open notepad").action == ACTION_IGNORE


def test_alt_r_still_strips_spoken_wake_word(gate):
    gate.arm_direct()
    assert gate.evaluate("Altron open notepad").query == "open notepad"


def test_alt_r_arm_expires(gate, clock):
    gate.arm_direct(window=5)
    clock.t += 6
    assert gate.evaluate("open notepad").action == ACTION_IGNORE


def test_wake_word_not_required_when_disabled(clock):
    gate = WakeGate(require_wake_word=False, clock=clock)
    assert gate.evaluate("open notepad").query == "open notepad"
    assert gate.evaluate("Altron open notepad").query == "open notepad"
    assert gate.evaluate("Altron").action == ACTION_ACK


def test_hotkey_calls_back_and_unregisters_cleanly():
    import sys
    if sys.platform != "win32":
        pytest.skip("Win32 only")
    import ctypes
    hk = GlobalHotkey(lambda: None, vk=0x7B, modifiers=0x0001 | 0x0004, label="Alt+Shift+F12")
    if not hk.start():
        pytest.skip("hotkey already taken on this machine")
    assert hk.registered
    hk.stop()
    assert not hk.registered
    # Registration is free again after stop().
    user32 = ctypes.windll.user32
    assert user32.RegisterHotKey(None, 0x1234, 0x0001 | 0x0004, 0x7B)
    user32.UnregisterHotKey(None, 0x1234)
