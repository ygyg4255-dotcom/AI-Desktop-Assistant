"""PyInstaller packaging script for AI Desktop Assistant.

Builds a standalone, windowed Windows executable with system tray support.
Usage:
    python build_exe.py             # Builds onedir bundle (recommended for fast tray startup)
    python build_exe.py --onefile   # Builds single portable .exe
"""
import sys
import shutil
from pathlib import Path
import PyInstaller.__main__


def build():
    """Build the AI Desktop Assistant executable."""
    root_dir = Path(__file__).resolve().parent
    entry_point = root_dir / "src" / "app.py"
    dist_dir = root_dir / "dist"
    build_dir = root_dir / "build"
    app_name = "AIDesktopAssistant"

    onefile = "--onefile" in sys.argv

    print("=" * 60)
    print(f"Building {app_name} with PyInstaller")
    print(f"Mode: {'Single File (--onefile)' if onefile else 'Directory Bundle (--onedir)'}")
    print(f"Entry Point: {entry_point}")
    print("=" * 60)

    # PyInstaller arguments
    args = [
        str(entry_point),
        f"--name={app_name}",
        "--windowed",      # No console window (system tray agent)
        "--clean",
        "--noconfirm",
        f"--distpath={dist_dir}",
        f"--workpath={build_dir}",
        # Add source package directory
        f"--add-data={root_dir / 'src'}{';'}src",
        # Hidden imports
        "--hidden-import=PySide6",
        "--hidden-import=PySide6.QtCore",
        "--hidden-import=PySide6.QtGui",
        "--hidden-import=PySide6.QtWidgets",
        "--hidden-import=sqlite3",
        "--hidden-import=psutil",
        "--hidden-import=ctypes",
        "--hidden-import=winreg",
        "--hidden-import=pyttsx3",
        "--hidden-import=pyttsx3.drivers",
        "--hidden-import=pyttsx3.drivers.sapi5",
        "--hidden-import=speech_recognition",
        "--hidden-import=pyaudio",
        "--hidden-import=comtypes",
    ]

    if onefile:
        args.append("--onefile")
    else:
        args.append("--onedir")

    # Add template configuration if present
    env_example = root_dir / ".env.example"
    if env_example.exists():
        args.append(f"--add-data={env_example}{';'}.")

    print("\nRunning PyInstaller...")
    PyInstaller.__main__.run(args)

    # Verification
    if onefile:
        exe_path = dist_dir / f"{app_name}.exe"
    else:
        exe_path = dist_dir / app_name / f"{app_name}.exe"
        # Copy .env.example into output folder for user template configuration
        if env_example.exists():
            shutil.copy(env_example, dist_dir / app_name / ".env.example")

    print("\n" + "=" * 60)
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"[SUCCESS] Executable built successfully!")
        print(f"Path: {exe_path}")
        print(f"Size: {size_mb:.2f} MB")
        print("=" * 60)
        return True
    else:
        print(f"[FAIL] Executable not found at {exe_path}")
        print("=" * 60)
        return False


if __name__ == "__main__":
    success = build()
    sys.exit(0 if success else 1)
