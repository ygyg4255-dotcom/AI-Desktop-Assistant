"""Locations for runtime state (kept out of the project folder / OneDrive sync, audit S8)."""
import os
import shutil
from pathlib import Path

_LEGACY_ROOT = Path(__file__).resolve().parent.parent.parent


def get_runtime_dir() -> Path:
    """Per-user data dir. ALTRON_DATA_DIR overrides it (used by tests)."""
    override = os.environ.get("ALTRON_DATA_DIR")
    return Path(override) if override else Path.home() / ".ai-desktop-assistant"


def get_desktop_dir() -> Path:
    """Real Desktop folder: Windows Shell API, then OneDrive/Desktop, then ~/Desktop."""
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            # 0x0010 = CSIDL_DESKTOPDIRECTORY; returns S_OK (0) on success
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0 and Path(buf.value).is_dir():
                return Path(buf.value)
        except (OSError, AttributeError):
            pass  # fall through to the home-based guesses
    home = Path.home()
    cands = [home / "OneDrive" / "Desktop", home / "Desktop"]
    return next((c for c in cands if c.is_dir()), cands[-1])


def runtime_file(name: str) -> Path:
    """Path of a runtime file in the data dir, migrating a legacy project-root copy once."""
    target = get_runtime_dir() / name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        legacy = _LEGACY_ROOT / name
        if not target.exists() and legacy.is_file() and legacy.resolve() != target.resolve():
            shutil.move(str(legacy), str(target))
    except OSError:
        pass
    return target
