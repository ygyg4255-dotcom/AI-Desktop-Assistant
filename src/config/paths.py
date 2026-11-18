"""Locations for runtime state (kept out of the project folder / OneDrive sync, audit S8)."""
import os
import shutil
from pathlib import Path

_LEGACY_ROOT = Path(__file__).resolve().parent.parent.parent


def get_runtime_dir() -> Path:
    """Per-user data dir. ALTRON_DATA_DIR overrides it (used by tests)."""
    override = os.environ.get("ALTRON_DATA_DIR")
    return Path(override) if override else Path.home() / ".ai-desktop-assistant"


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
