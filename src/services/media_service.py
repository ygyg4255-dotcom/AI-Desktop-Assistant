"""Windows global media control.

Playback commands are sent as media keys (``keybd_event`` with the VK_MEDIA_* codes), which
reach whichever app currently owns the media session, so they need no extra packages.

Track information comes from the Global System Media Transport Controls (GSMTC) through the
optional ``winsdk`` package. When it is not installed, or no session exists, the caller gets
an explicit "not available" result instead of an error, and the playback keys still work.
"""
import asyncio
from typing import Callable, Dict, Optional

from src.config.log import get_logger

logger = get_logger(__name__)

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

PLAYBACK_KEYS = {
    "play_pause": VK_MEDIA_PLAY_PAUSE,
    "next": VK_MEDIA_NEXT_TRACK,
    "previous": VK_MEDIA_PREV_TRACK,
    "stop": VK_MEDIA_STOP,
}

# GSMTC PlaybackStatus enum values (Windows.Media.Control).
_PLAYBACK_STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}

_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002


class MediaError(RuntimeError):
    """A media command could not be carried out on this system."""


def send_media_key(vk_code: int) -> None:
    """Press and release one media key (Windows only)."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
    except (ImportError, AttributeError) as err:
        raise MediaError("Media keys are only available on Windows.") from err
    user32.keybd_event(vk_code, 0, _KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(vk_code, 0, _KEYEVENTF_EXTENDEDKEY | _KEYEVENTF_KEYUP, 0)


def press_playback(action: str, sender: Optional[Callable[[int], None]] = None) -> None:
    """Send the media key for ``action`` (one of PLAYBACK_KEYS)."""
    vk = PLAYBACK_KEYS.get(action)
    if vk is None:
        raise MediaError(f"Unknown playback action: {action!r}.")
    (sender or send_media_key)(vk)  # resolved per call so tests can stub it


async def _read_gsmtc_session() -> Optional[Dict[str, object]]:
    from winsdk.windows.media.control import (  # optional dependency, imported lazily
        GlobalSystemMediaTransportControlsSessionManager as Manager,
    )
    manager = await Manager.request_async()
    session = manager.get_current_session()
    if session is None:
        return None
    props = await session.try_get_media_properties_async()
    status = session.get_playback_info().playback_status
    return {
        "title": str(props.title or ""),
        "artist": str(props.artist or ""),
        "album": str(props.album_title or ""),
        "status": _PLAYBACK_STATUS.get(int(status), "unknown"),
    }


def _default_reader() -> Optional[Dict[str, object]]:
    return asyncio.run(_read_gsmtc_session())


def now_playing(reader: Callable[[], Optional[Dict[str, object]]] = _default_reader) -> Dict[str, object]:
    """Describe the current media session. Never raises: unavailability is a result."""
    try:
        info = reader()
    except ImportError:
        return {"available": False,
                "message": "Track info needs the optional 'winsdk' package; playback keys still work."}
    except Exception as err:  # GSMTC can fail in many Windows-specific ways; keep the tool usable
        logger.debug("GSMTC query failed: %s", err)
        return {"available": False, "message": "Could not read the current media session."}
    if not info:
        return {"available": False, "message": "No media session is playing right now."}
    return {"available": True, **info}
