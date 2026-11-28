"""window_close closes exactly one window; swap exchanges both split windows in one call."""
import sys
import types

import pytest

from src.agent import tools
from src.agent.permissions import PermissionGate


@pytest.fixture
def fake_win(monkeypatch):
    posted, moved = [], []
    rects = {}
    gui = types.SimpleNamespace(
        PostMessage=lambda h, m, w, l: posted.append((h, m)),
        IsIconic=lambda h: False,
        GetWindowRect=lambda h: rects[h],
        MoveWindow=lambda h, x, y, w, ht, r: moved.append((h, x, w)),
    )
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    monkeypatch.setitem(sys.modules, "win32con", types.SimpleNamespace(WM_CLOSE=0x10))
    wins = []
    monkeypatch.setattr(tools, "_visible_windows", lambda: list(wins))
    return types.SimpleNamespace(posted=posted, moved=moved, rects=rects, wins=wins)


def test_window_close_not_gated():
    assert not PermissionGate().is_dangerous("window_close")
    assert not tools.handle_window_close({"title": "x"}).requires_permission


def test_window_close_one_match(fake_win):
    fake_win.wins += [(1, "old - Visual Studio Code"), (2, "new - Visual Studio Code")]
    r = tools.handle_window_close({"title": "old"}, approved=True)
    assert r.success and fake_win.posted == [(1, 0x10)]


def test_window_close_no_match(fake_win):
    fake_win.wins.append((1, "Notepad"))
    r = tools.handle_window_close({"title": "chrome"}, approved=True)
    assert not r.success and "No open window" in r.error and not fake_win.posted


def test_window_close_ambiguous_closes_nothing(fake_win):
    fake_win.wins += [(1, "a - Code"), (2, "b - Code")]
    r = tools.handle_window_close({"title": "code"}, approved=True)
    assert not r.success and not fake_win.posted


def test_swap_moves_both_windows(fake_win, monkeypatch):
    import ctypes
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(
        user32=types.SimpleNamespace(GetSystemMetrics=lambda i: 1920 if i == 0 else 1080)), raising=False)
    fake_win.wins += [(1, "Left App"), (2, "Right App")]
    fake_win.rects.update({1: (0, 0, 960, 1040), 2: (960, 0, 1920, 1040)})
    r = tools.handle_move_application_window({"position": "swap"})
    assert r.success
    assert sorted(fake_win.moved) == [(1, 960, 960), (2, 0, 960)]


def test_swap_needs_two_split_windows(fake_win, monkeypatch):
    import ctypes
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(
        user32=types.SimpleNamespace(GetSystemMetrics=lambda i: 1920 if i == 0 else 1080)), raising=False)
    fake_win.wins.append((1, "Only"))
    fake_win.rects[1] = (0, 0, 960, 1040)
    r = tools.handle_move_application_window({"position": "swap"})
    assert not r.success and not fake_win.moved
