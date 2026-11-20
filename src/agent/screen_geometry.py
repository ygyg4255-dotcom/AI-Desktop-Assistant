"""Screen geometry for GUI automation: DPI awareness and screenshot-to-screen coordinate mapping.

The model picks click coordinates on the image it was shown (``capture_and_inspect_screen``
sends a JPEG downscaled to at most 1280 px wide). ``gui_click`` needs real screen pixels, so
the last screenshot's size is remembered here and clicks are scaled from the image space to
the screen space. Without DPI awareness, pyautogui reports logical pixels while ImageGrab
captures physical pixels on scaled displays, which is what made clicks miss.
"""
import ctypes
import sys
import threading
from typing import Optional, Tuple

from src.config.log import get_logger

logger = get_logger(__name__)

Size = Tuple[int, int]

_E_ACCESSDENIED = -2147024891  # HRESULT: DPI awareness was already set for this process

_lock = threading.Lock()
_dpi_state: Optional[bool] = None  # None = not attempted yet
_last_image_size: Optional[Size] = None  # size of the screenshot the model was last shown


def ensure_dpi_aware() -> bool:
    """Make this process per-monitor DPI aware (Windows only). Idempotent and never raises.

    Tries ``SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)``, falls back to the
    older ``SetProcessDPIAware()``. Returns True when awareness is (now) in effect.
    """
    global _dpi_state
    with _lock:
        if _dpi_state is not None:
            return _dpi_state
        _dpi_state = _set_dpi_awareness()
        return _dpi_state


def _set_dpi_awareness() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        windll = ctypes.windll  # type: ignore[attr-defined]
    except AttributeError as e:
        logger.debug("DPI awareness unavailable: %s", e)
        return False
    try:
        hr = windll.shcore.SetProcessDpiAwareness(2)
        if hr == 0 or hr == _E_ACCESSDENIED:
            return True
        logger.debug("SetProcessDpiAwareness returned HRESULT %s; trying legacy API", hr)
    except (OSError, AttributeError) as e:
        logger.debug("shcore DPI awareness failed: %s", e)
    try:
        return bool(windll.user32.SetProcessDPIAware())
    except (OSError, AttributeError) as e:
        logger.debug("Legacy DPI awareness failed: %s", e)
        return False


def screen_size() -> Optional[Size]:
    """Primary screen size in physical pixels, or None when it cannot be read."""
    ensure_dpi_aware()
    try:
        import pyautogui
        width, height = pyautogui.size()
        return int(width), int(height)
    except Exception as e:  # no display, or pyautogui missing
        logger.debug("Could not read screen size: %s", e)
        return None


def record_screenshot(image_size: Optional[Size]) -> None:
    """Remember the size of the screenshot just shown to the model (None clears it)."""
    global _last_image_size
    with _lock:
        _last_image_size = (int(image_size[0]), int(image_size[1])) if image_size else None


def last_screenshot_size() -> Optional[Size]:
    with _lock:
        return _last_image_size


def map_to_screen(x: int, y: int, screen: Size) -> Size:
    """Map a point in the last screenshot's pixel space to real screen pixels.

    With no screenshot on record the point is already a screen point and is returned as is.
    The result may fall outside the screen; the caller rejects that.
    """
    image = last_screenshot_size()
    if not image:
        return x, y
    image_w, image_h = image
    screen_w, screen_h = screen
    return round(x * screen_w / image_w), round(y * screen_h / image_h)
