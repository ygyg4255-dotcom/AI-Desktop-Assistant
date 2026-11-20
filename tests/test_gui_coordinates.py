"""Screenshot-to-screen coordinate mapping for gui_click, and DPI-aware initialisation."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from src.agent import screen_geometry as geo
from src.agent import tools as tools_mod
from src.agent.tools import create_default_registry


@pytest.fixture(autouse=True)
def _reset_geometry(monkeypatch):
    """Module-level geometry state must not leak between tests."""
    monkeypatch.setattr(geo, "_last_image_size", None)
    monkeypatch.setattr(geo, "_dpi_state", None)
    yield
    geo.record_screenshot(None)


@pytest.fixture
def pg(monkeypatch):
    mock = MagicMock()
    mock.FAILSAFE = True
    mock.PAUSE = 0.1
    mock.size.return_value = (1920, 1080)
    monkeypatch.setitem(sys.modules, "pyautogui", mock)
    monkeypatch.setattr(geo, "ensure_dpi_aware", lambda: True)
    return mock


# ------------------------------------------------------------- pure mapping

def test_click_on_1280x720_screenshot_maps_to_1920x1080_screen(pg):
    geo.record_screenshot((1280, 720))

    result = create_default_registry().get("gui_click").run({"x": 68, "y": 168})

    assert result.success
    pg.click.assert_called_once_with(x=102, y=252, button="left", clicks=1)
    assert "from screenshot point (68, 168)" in result.output["message"]


def test_mapping_is_identity_without_a_recorded_screenshot():
    assert geo.map_to_screen(68, 168, (1920, 1080)) == (68, 168)


def test_mapping_is_identity_when_image_matches_screen():
    geo.record_screenshot((1920, 1080))
    assert geo.map_to_screen(500, 300, (1920, 1080)) == (500, 300)


def test_mapping_rounds_to_nearest_pixel():
    geo.record_screenshot((1280, 720))
    # 1 * 1920 / 1280 = 1.5 -> 2 (round half to even -> 2); 3 * 1080 / 720 = 4.5 -> 4
    assert geo.map_to_screen(1, 3, (1920, 1080)) == (2, 4)


def test_clearing_the_record_restores_raw_coordinates():
    geo.record_screenshot((1280, 720))
    geo.record_screenshot(None)
    assert geo.last_screenshot_size() is None
    assert geo.map_to_screen(68, 168, (1920, 1080)) == (68, 168)


def test_point_past_the_screenshot_edge_is_rejected_after_mapping(pg):
    geo.record_screenshot((1280, 720))
    # 1300 is outside the screenshot, and scales to 1950, which is outside the 1920-wide screen.
    result = create_default_registry().get("gui_click").run({"x": 1300, "y": 10})
    assert not result.success and "outside" in result.error
    pg.click.assert_not_called()


def test_scaling_does_not_change_the_recorded_screenshot(pg):
    geo.record_screenshot((1280, 720))
    create_default_registry().get("gui_click").run({"x": 68, "y": 168})
    assert geo.last_screenshot_size() == (1280, 720)


# --------------------------------------------------- capture records geometry

@pytest.fixture
def fake_screen(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    import PIL.ImageGrab
    monkeypatch.setattr(PIL.ImageGrab, "grab", lambda **kw: Image.new("RGB", (2560, 1440), (10, 120, 200)))
    monkeypatch.setattr(geo, "screen_size", lambda: (1920, 1080))


def test_capture_reports_sizes_and_records_the_shown_image(fake_screen, pg):
    result = tools_mod.handle_capture_and_inspect_screen({"send_to_telegram": False})

    assert result.success and result.image is not None
    assert result.output["screenshot_size"] == [1280, 720]
    assert result.output["screen_size"] == [1920, 1080]
    assert geo.last_screenshot_size() == (1280, 720)

    # the model now clicks on the image it was shown; the tool scales it to the screen
    create_default_registry().get("gui_click").run({"x": 68, "y": 168})
    pg.click.assert_called_once_with(x=102, y=252, button="left", clicks=1)


def test_capture_without_an_image_clears_the_record(fake_screen, monkeypatch):
    geo.record_screenshot((1280, 720))
    monkeypatch.setattr("src.agent.tools.compress_image_file", MagicMock(side_effect=OSError("boom")))

    result = tools_mod.handle_capture_and_inspect_screen({"send_to_telegram": False})

    assert result.success and result.image is None
    assert result.output["screenshot_size"] is None
    assert geo.last_screenshot_size() is None


# ------------------------------------------------------------ DPI awareness

class _FakeUser32:
    def __init__(self, ok=True):
        self.calls = 0
        self._ok = ok

    def SetProcessDPIAware(self):
        self.calls += 1
        return 1 if self._ok else 0


class _FakeShcore:
    def __init__(self, hr=0, raises=None):
        self.hr, self.raises, self.calls = hr, raises, []

    def SetProcessDpiAwareness(self, level):
        self.calls.append(level)
        if self.raises:
            raise self.raises
        return self.hr


class _FakeWinDll:
    def __init__(self, shcore, user32):
        self.shcore, self.user32 = shcore, user32


def _install_windll(monkeypatch, windll):
    import ctypes
    monkeypatch.setattr(geo.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)


def test_dpi_awareness_uses_per_monitor_mode_first(monkeypatch):
    shcore, user32 = _FakeShcore(hr=0), _FakeUser32()
    _install_windll(monkeypatch, _FakeWinDll(shcore, user32))

    assert geo.ensure_dpi_aware() is True
    assert shcore.calls == [2] and user32.calls == 0


def test_already_dpi_aware_counts_as_success(monkeypatch):
    shcore, user32 = _FakeShcore(hr=-2147024891), _FakeUser32()  # E_ACCESSDENIED: already set
    _install_windll(monkeypatch, _FakeWinDll(shcore, user32))

    assert geo.ensure_dpi_aware() is True
    assert user32.calls == 0


def test_falls_back_to_legacy_api_when_shcore_is_missing(monkeypatch):
    user32 = _FakeUser32(ok=True)
    _install_windll(monkeypatch, _FakeWinDll(shcore=None, user32=user32))

    assert geo.ensure_dpi_aware() is True
    assert user32.calls == 1


def test_dpi_init_never_raises_and_reports_failure(monkeypatch):
    shcore = _FakeShcore(raises=OSError("no shcore"))
    user32 = _FakeUser32(ok=False)
    _install_windll(monkeypatch, _FakeWinDll(shcore, user32))

    assert geo.ensure_dpi_aware() is False


def test_dpi_init_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr(geo.sys, "platform", "linux")
    assert geo.ensure_dpi_aware() is False


def test_dpi_init_runs_once_and_is_cached(monkeypatch):
    shcore, user32 = _FakeShcore(hr=0), _FakeUser32()
    _install_windll(monkeypatch, _FakeWinDll(shcore, user32))

    geo.ensure_dpi_aware()
    geo.ensure_dpi_aware()
    assert shcore.calls == [2]
