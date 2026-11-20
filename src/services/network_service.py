"""Wi-Fi and Bluetooth status/control via fixed Windows commands (netsh / PowerShell).

Security (audit S7): every command is an argument list run without a shell. The PowerShell
scripts are constants with no interpolated user text. The only user-supplied value, a Wi-Fi
SSID, must match a profile that already exists on this PC and is passed as a single argv item.

Toggling an adapter normally needs an elevated (administrator) Altron. When it is refused,
the error says so rather than failing silently.
"""
import json
import re
import shutil
import subprocess
from typing import List

from src.config.log import get_logger

logger = get_logger(__name__)

SSID_MAX_CHARS = 32
_COMMAND_TIMEOUT = 25
_CREATE_NO_WINDOW = 0x08000000

# Bluetooth radio adapters are USB- or PCI-attached PnP devices in the Bluetooth class.
_BT_ADAPTERS = r"Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -match '^(USB|PCI)\\' }"
_BT_STATUS_SCRIPT = (
    "$svc = Get-Service -Name bthserv -ErrorAction SilentlyContinue; "
    f"$adapters = @({_BT_ADAPTERS} | Select-Object FriendlyName, Status); "
    "[pscustomobject]@{ service = if ($svc) { [string]$svc.Status } else { 'NotFound' }; "
    "adapters = $adapters } | ConvertTo-Json -Compress -Depth 3"
)
_BT_DEVICES_SCRIPT = (
    "@(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
    "Where-Object { $_.FriendlyName -and $_.Status -eq 'OK' } | "
    "Select-Object FriendlyName, InstanceId) | ConvertTo-Json -Compress"
)


class NetworkError(RuntimeError):
    """A network or Bluetooth command failed or is not possible on this PC."""


def _run(argv: List[str]) -> str:
    exe = shutil.which(argv[0])
    if not exe:
        raise NetworkError(f"'{argv[0]}' is not available on this PC.")
    try:
        proc = subprocess.run(
            [exe, *argv[1:]], capture_output=True, text=True,
            timeout=_COMMAND_TIMEOUT, creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as err:
        raise NetworkError(f"'{argv[0]}' timed out.") from err
    output = (proc.stdout or "").strip()
    if proc.returncode != 0:
        detail = (proc.stderr or output or "").strip()[:300]
        if "access is denied" in detail.lower() or "requires elevation" in detail.lower():
            raise NetworkError("Windows refused this change: Altron must run as administrator for it.")
        raise NetworkError(detail or f"'{argv[0]}' failed with exit code {proc.returncode}.")
    return output


def _powershell(script: str) -> str:
    return _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])


def _parse_key_values(text: str) -> dict:
    """First occurrence of each 'Key : value' line of netsh output (keys are case-folded)."""
    found = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([^:]+?)\s*:\s*(.*?)\s*$", line)
        if match:
            found.setdefault(match.group(1).casefold(), match.group(2))
    return found


def wifi_status() -> dict:
    values = _parse_key_values(_run(["netsh", "wlan", "show", "interfaces"]))
    name = values.get("name", "")
    if not name:
        raise NetworkError("No Wi-Fi adapter was found on this PC.")
    return {
        "adapter": name,
        "state": values.get("state", "unknown"),
        "ssid": values.get("ssid", ""),
        "signal": values.get("signal", ""),
    }


def wifi_set_enabled(enabled: bool) -> str:
    adapter = wifi_status()["adapter"]
    _run(["netsh", "interface", "set", "interface", f'name="{adapter}"',
          "admin=enabled" if enabled else "admin=disabled"])
    return adapter


def wifi_known_networks() -> List[str]:
    text = _run(["netsh", "wlan", "show", "profiles"])
    names = []
    for line in text.splitlines():
        match = re.search(r"All User Profile\s*:\s*(.+?)\s*$", line)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def validate_ssid(ssid: str) -> str:
    name = str(ssid or "").strip()
    if not name:
        raise NetworkError("A Wi-Fi network name (SSID) is required.")
    if len(name) > SSID_MAX_CHARS or any(ord(ch) < 32 for ch in name):
        raise NetworkError("That is not a valid Wi-Fi network name.")
    return name


def wifi_connect(ssid: str) -> str:
    """Connect to a Wi-Fi network that already has a saved profile on this PC."""
    name = validate_ssid(ssid)
    if name not in wifi_known_networks():
        raise NetworkError(f"No saved Wi-Fi profile named {name!r}. Only known networks can be joined.")
    _run(["netsh", "wlan", "connect", f'name="{name}"'])
    return name


def bluetooth_status() -> dict:
    data = json.loads(_powershell(_BT_STATUS_SCRIPT) or "{}")
    adapters = data.get("adapters") or []
    if isinstance(adapters, dict):
        adapters = [adapters]
    return {
        "service": data.get("service", "unknown"),
        "adapters": [{"name": a.get("FriendlyName", ""), "status": a.get("Status", "")} for a in adapters],
    }


def bluetooth_devices() -> List[dict]:
    raw = _powershell(_BT_DEVICES_SCRIPT)
    items = json.loads(raw) if raw else []
    if isinstance(items, dict):
        items = [items]
    return [{"name": d.get("FriendlyName", ""), "id": d.get("InstanceId", "")} for d in items]


def bluetooth_set_enabled(enabled: bool) -> int:
    verb = "Enable-PnpDevice" if enabled else "Disable-PnpDevice"
    script = (f"$n = 0; {_BT_ADAPTERS} | ForEach-Object {{ {verb} -InstanceId $_.InstanceId "
              "-Confirm:$false -ErrorAction Stop; $n++ }; $n")
    raw = _powershell(script)
    try:
        return int(raw.strip() or 0)
    except ValueError:
        return 0
