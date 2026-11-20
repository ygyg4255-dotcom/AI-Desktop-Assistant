"""Native Windows desktop notification (tray balloon / toast) for reminders.

Uses a fixed PowerShell script with WinForms ``NotifyIcon.ShowBalloonTip``, which Windows 10/11
shows as a toast in the notification centre. The title and text travel in environment
variables, never in the script or the argument list, so reminder text cannot inject commands
(audit S7). ``show_desktop_notification`` never raises; it returns False when it could not show
the notification. Call it from a background thread: it waits for the balloon to be displayed.
"""
import os
import shutil
import subprocess
import sys

from src.config.log import get_logger, redact_secrets

logger = get_logger(__name__)

TITLE_MAX = 63    # NotifyIcon balloon title limit
TEXT_MAX = 255    # NotifyIcon balloon text limit
BALLOON_MS = 10000
PROCESS_TIMEOUT_S = 25.0

# Fixed script: no reminder content is interpolated into it.
_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "Add-Type -AssemblyName System.Drawing; "
    "$n = New-Object System.Windows.Forms.NotifyIcon; "
    "$n.Icon = [System.Drawing.SystemIcons]::Information; "
    "$n.BalloonTipIcon = 'Info'; "
    "$n.BalloonTipTitle = $env:ALTRON_TOAST_TITLE; "
    "$n.BalloonTipText = $env:ALTRON_TOAST_TEXT; "
    "$n.Visible = $true; "
    f"$n.ShowBalloonTip({BALLOON_MS}); "
    f"Start-Sleep -Milliseconds {BALLOON_MS + 1000}; "
    "$n.Dispose()"
)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _powershell() -> "str | None":
    return shutil.which("powershell.exe") or shutil.which("powershell")


def show_desktop_notification(title: str, message: str) -> bool:
    """Show a native notification. Returns True when the PowerShell helper ran successfully."""
    if sys.platform != "win32":
        logger.debug("Desktop notifications are only implemented for Windows")
        return False
    exe = _powershell()
    if not exe:
        logger.debug("No PowerShell found; desktop notification skipped")
        return False
    env = dict(os.environ)
    env["ALTRON_TOAST_TITLE"] = _clip(redact_secrets(title or "Altron"), TITLE_MAX)
    env["ALTRON_TOAST_TEXT"] = _clip(redact_secrets(message or ""), TEXT_MAX)
    args = [exe, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _SCRIPT]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            args, env=env, capture_output=True, timeout=PROCESS_TIMEOUT_S,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as err:
        logger.debug("Desktop notification failed: %s", redact_secrets(str(err)))
        return False
    if result.returncode != 0:
        logger.debug("Desktop notification exited %s: %s", result.returncode,
                     redact_secrets(result.stderr.decode("utf-8", "replace")[:300]))
        return False
    return True
