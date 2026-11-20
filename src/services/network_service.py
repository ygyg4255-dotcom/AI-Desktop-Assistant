"""Wi-Fi and Bluetooth status/control via fixed Windows commands (netsh / PowerShell).

Security (audit S7): every command is an argument list run without a shell. The PowerShell
scripts are constants with no interpolated user text. The only user-supplied value, a Wi-Fi
SSID, must match a profile that already exists on this PC and is passed as a single argv item.

Toggling an adapter normally needs an elevated (administrator) Altron. When it is refused,
the error says so rather than failing silently.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional
from xml.sax.saxutils import escape as _xml_escape

from src.config.log import get_logger
from src.config.paths import runtime_file

logger = get_logger(__name__)

# Saved Wi-Fi passwords ({ssid: password}) live in the runtime dir, never in the project folder.
# This is the one place Altron keeps a credential, and only for Wi-Fi networks the user names.
VAULT_FILENAME = "wifi_vault.json"
PASSWORD_MIN_CHARS = 8    # WPA2/WPA3 passphrase length limits
PASSWORD_MAX_CHARS = 63
_vault_lock = threading.RLock()

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


# --- Saved Wi-Fi passwords (wifi_vault.json) ----------------------------------

def vault_path() -> Path:
    return runtime_file(VAULT_FILENAME)


def _load_vault() -> dict:
    """Read the vault. A corrupt file is moved aside (never overwritten) and reported."""
    path = vault_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise ValueError("unexpected structure")
        return data
    except (OSError, ValueError) as err:
        aside = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        logger.warning("Wi-Fi vault unreadable (%s); moving it aside to %s", err, aside.name)
        try:
            os.replace(path, aside)
        except OSError as move_err:
            raise NetworkError("The saved Wi-Fi password store is unreadable and could not be moved aside.") from move_err
        return {}


def _save_vault(data: dict) -> None:
    """Atomic write: temp file in the same folder, then os.replace."""
    path = vault_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".wifi_vault-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        _remove_quietly(tmp)
        raise


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as err:
        logger.warning("Could not delete temporary Wi-Fi file: %s", err)


def vault_get(ssid: str) -> Optional[str]:
    with _vault_lock:
        return _load_vault().get(validate_ssid(ssid))


def vault_put(ssid: str, password: str) -> None:
    name = validate_ssid(ssid)
    with _vault_lock:
        data = _load_vault()
        data[name] = password
        _save_vault(data)


def validate_password(password: str) -> str:
    text = str(password or "")
    if not PASSWORD_MIN_CHARS <= len(text) <= PASSWORD_MAX_CHARS:
        raise NetworkError(
            f"A Wi-Fi password must be {PASSWORD_MIN_CHARS} to {PASSWORD_MAX_CHARS} characters."
        )
    if any(ord(ch) < 32 for ch in text):
        raise NetworkError("That Wi-Fi password contains control characters.")
    return text


def _profile_password(text: str) -> Optional[str]:
    """The 'Key Content' value from `netsh wlan show profile ... key=clear` output."""
    for line in text.splitlines():
        match = re.match(r"^\s*Key Content\s*:\s?(.*?)\s*$", line)
        if match:
            return match.group(1) or None
    return None


def _read_profile_password(name: str) -> str:
    text = _run(["netsh", "wlan", "show", "profile", f'name="{name}"', "key=clear"])
    password = _profile_password(text)
    if not password:
        raise NetworkError(
            f"Windows did not return a password for {name!r}. It may be an open network, "
            "or Altron may need to run as administrator to read it."
        )
    return password


def wifi_get_password(ssid: str) -> str:
    """Read a saved profile's Wi-Fi password, store it in the vault, and return it."""
    name = validate_ssid(ssid)
    password = _read_profile_password(name)
    vault_put(name, password)
    return password


def wifi_forget(ssid: str) -> dict:
    """Back up the password to the vault if it can be read, then delete the Windows profile.

    If the password is readable but the vault cannot be written, the profile is kept.
    """
    name = validate_ssid(ssid)
    backed_up = False
    reason = ""
    try:
        vault_put(name, _read_profile_password(name))
        backed_up = True
    except NetworkError as err:
        reason = str(err)
        logger.debug("No password backed up for %r: %s", name, err)
    _run(["netsh", "wlan", "delete", "profile", f'name="{name}"'])
    return {"ssid": name, "backed_up": backed_up, "reason": reason}


def build_wpa2_profile_xml(ssid: str, password: str) -> str:
    """WLANProfile XML for a WPA2-Personal (AES) network. WPA3-transition networks accept it too.

    SSID and passphrase are XML-escaped (including quotes), so they cannot break out of their tags.
    """
    name = _xml_escape(validate_ssid(ssid), {'"': "&quot;", "'": "&apos;"})
    key = _xml_escape(validate_password(password), {'"': "&quot;", "'": "&apos;"})
    return (
        '<?xml version="1.0"?>\n'
        '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">\n'
        f"  <name>{name}</name>\n"
        "  <SSIDConfig>\n"
        f"    <SSID><name>{name}</name></SSID>\n"
        "  </SSIDConfig>\n"
        "  <connectionType>ESS</connectionType>\n"
        "  <connectionMode>auto</connectionMode>\n"
        "  <MSM>\n"
        "    <security>\n"
        "      <authEncryption>\n"
        "        <authentication>WPA2PSK</authentication>\n"
        "        <encryption>AES</encryption>\n"
        "        <useOneX>false</useOneX>\n"
        "      </authEncryption>\n"
        "      <sharedKey>\n"
        "        <keyType>passPhrase</keyType>\n"
        "        <protected>false</protected>\n"
        f"        <keyMaterial>{key}</keyMaterial>\n"
        "      </sharedKey>\n"
        "    </security>\n"
        "  </MSM>\n"
        "</WLANProfile>\n"
    )


def wifi_connect_new(ssid: str, password: Optional[str] = None) -> str:
    """Add a WPA2/WPA3 profile for ssid, connect, and store the password in the vault.

    With no password, the vault's password for ssid is used (reconnecting after a forget).
    The temporary XML holding the passphrase is deleted right after netsh reads it.
    """
    name = validate_ssid(ssid)
    if password:
        key = validate_password(password)
    else:
        key = vault_get(name)
        if not key:
            raise NetworkError(
                f"No password is stored for {name!r}. Give the Wi-Fi password to connect to it."
            )
    xml = build_wpa2_profile_xml(name, key)
    fd, tmp = tempfile.mkstemp(suffix=".xml", prefix="altron_wifi_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(xml)
        _run(["netsh", "wlan", "add", "profile", f'filename="{tmp}"'])
    finally:
        _remove_quietly(tmp)
    vault_put(name, key)
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
