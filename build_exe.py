"""PyInstaller packaging script for Altron (AI Desktop Assistant).

Bundles the official runtime: the headless ``main.py`` orchestrator plus the
Flask HUD (``ui_server.py`` serving ``ui/``). The build runs as a console app
so logs are visible and Ctrl+C shuts it down; the full log is also written to
``~/.ai-desktop-assistant/altron.log``.

Usage:
    python build_exe.py             # onedir bundle (recommended, fast startup)
    python build_exe.py --onefile   # single portable .exe
"""
import logging
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("build_exe")

APP_NAME = "AIDesktopAssistant"

# Modules PyInstaller's static analysis can miss (lazy, driver-style or
# platform-specific imports).
HIDDEN_IMPORTS = [
    # HUD server (imported by main.py; listed explicitly for safety)
    "ui_server",
    "flask",
    # agent + config modules added in the S5/S8/logging phases
    "src.agent.approvals",
    "src.config.paths",
    "src.config.log",
    "src.services.signals",
    "src.services.hud_status",
    # Windows / automation
    "sqlite3",
    "psutil",
    "ctypes",
    "winreg",
    "comtypes",
    "pythoncom",
    "win32com.client",
    "win32clipboard",
    "win32con",
    "win32gui",
    "win32process",
    "pyautogui",
    "PIL.ImageGrab",
    # Voice
    "pyttsx3.drivers",
    "pyttsx3.drivers.sapi5",
    "speech_recognition",
    "pyaudio",
    "sounddevice",
    "webrtcvad",
    "edge_tts",
    "pygame",
    # Legacy QThread wrappers in voice/telegram services still import these at module level
    "PySide6.QtCore",
    "PySide6.QtGui",
]

# Packages that ship native libraries or data files without a PyInstaller hook.
# faster_whisper: Python code + VAD model assets. ctranslate2: only its DLLs and
# data; --collect-all would also pull in ctranslate2.converters, which imports
# torch/transformers and adds gigabytes the runtime never uses.
COLLECT_ALL = ["faster_whisper"]
COLLECT_BINARIES_AND_DATA = ["ctranslate2"]

# Never bundle these: test suites, dev-only build tooling, and heavy ML stacks
# reachable only from optional code paths (model converters, the
# speech_recognition local-whisper backend).
EXCLUDES = [
    "pytest", "_pytest", "PyInstaller",
    "ctranslate2.converters",
    "torch", "torchaudio", "torchvision", "transformers", "tensorflow",
    "whisper", "speech_recognition.recognizers.whisper_local",
]


def build() -> bool:
    root_dir = Path(__file__).resolve().parent
    entry_point = root_dir / "main.py"
    ui_dir = root_dir / "ui"
    dist_dir = root_dir / "dist"
    build_dir = root_dir / "build"
    onefile = "--onefile" in sys.argv

    for required in ("index.html", "app.js", "style.css"):
        if not (ui_dir / required).is_file():
            log.error("[FAIL] Missing HUD asset: %s", ui_dir / required)
            return False

    log.info("=" * 60)
    log.info("Building %s with PyInstaller", APP_NAME)
    log.info("Mode: %s", "single file (--onefile)" if onefile else "directory bundle (--onedir)")
    log.info("Entry point: %s", entry_point)
    log.info("=" * 60)

    args = [
        str(entry_point),
        f"--name={APP_NAME}",
        "--console",
        "--clean",
        "--noconfirm",
        f"--distpath={dist_dir}",
        f"--workpath={build_dir}",
        f"--specpath={build_dir}",
        f"--paths={root_dir}",
        # ui_server.py serves <bundle>/ui next to its own module file
        f"--add-data={ui_dir};ui",
        "--onefile" if onefile else "--onedir",
    ]
    args += [f"--hidden-import={name}" for name in HIDDEN_IMPORTS]
    args += [f"--collect-all={name}" for name in COLLECT_ALL]
    for name in COLLECT_BINARIES_AND_DATA:
        args += [f"--collect-binaries={name}", f"--collect-data={name}"]
    args += [f"--exclude-module={name}" for name in EXCLUDES]

    env_example = root_dir / ".env.example"
    if env_example.exists():
        args.append(f"--add-data={env_example};.")

    log.info("Running PyInstaller...")
    PyInstaller.__main__.run(args)

    if onefile:
        exe_path = dist_dir / f"{APP_NAME}.exe"
    else:
        exe_path = dist_dir / APP_NAME / f"{APP_NAME}.exe"
        # Template next to the exe, where resolve_env_path() looks for .env
        if env_example.exists():
            shutil.copy(env_example, dist_dir / APP_NAME / ".env.example")

    log.info("=" * 60)
    if not exe_path.exists():
        log.error("[FAIL] Executable not found at %s", exe_path)
        return False
    log.info("[SUCCESS] Executable built: %s (%.2f MB)",
             exe_path, exe_path.stat().st_size / (1024 * 1024))
    log.info("=" * 60)
    return True


if __name__ == "__main__":
    sys.exit(0 if build() else 1)
