"""Shared test fixtures."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_screen_geometry():
    """The last-screenshot size is process-global; clear it so one test's capture never
    rescales another test's gui_click coordinates."""
    from src.agent import screen_geometry

    screen_geometry.record_screenshot(None)
    yield
    screen_geometry.record_screenshot(None)


@pytest.fixture(autouse=True)
def _no_ambient_approval_pin(monkeypatch):
    """A developer's real APPROVAL_PIN_HASH must not change how other tests approve."""
    from src.security import pin

    monkeypatch.setattr(pin, "configured_hash", lambda: "")
