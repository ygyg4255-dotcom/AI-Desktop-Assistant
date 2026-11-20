"""Steam helpers: local library scan, steam:// URI building and public store search.

The HTTP fetch is injected by the caller (``tools.py`` passes the SSRF-checked public opener),
so this module has no network access of its own. The only host it ever builds URLs for is
``store.steampowered.com``. Launching URIs is done by the tool layer, not here.
"""
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from src.config.log import get_logger

logger = get_logger(__name__)

STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/?term={term}&l=english&cc=US"
CART_URL = "https://store.steampowered.com/cart/"
MAX_QUERY_CHARS = 100
MAX_SEARCH_RESULTS = 10

_COMMON_STEAM_PATHS = (
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
    r"D:\Steam",
    r"D:\Program Files (x86)\Steam",
)
_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


class SteamError(ValueError):
    """The request is invalid or Steam could not be found."""


# ---------------------------------------------------------------- VDF / ACF parsing

def _unescape(value: str) -> str:
    return value.replace("\\\\", "\\").replace('\\"', '"')


def parse_vdf(text: str) -> Dict[str, Any]:
    """Parse Valve KeyValues text (libraryfolders.vdf, appmanifest_*.acf) into nested dicts."""
    root: Dict[str, Any] = {}
    stack: List[Dict[str, Any]] = [root]
    pending_key: Optional[str] = None
    for line in text.splitlines():
        line = re.sub(r"(?<!:)//.*$", "", line)  # strip // comments
        for match in _VDF_TOKEN.finditer(line):
            token, brace = match.group(1), match.group(2)
            if brace == "{":
                if pending_key is None:
                    continue
                child: Dict[str, Any] = {}
                stack[-1][pending_key] = child
                stack.append(child)
                pending_key = None
            elif brace == "}":
                if len(stack) > 1:
                    stack.pop()
                pending_key = None
            elif pending_key is None:
                pending_key = _unescape(token)
            else:
                stack[-1][pending_key] = _unescape(token)
                pending_key = None
    return root


# ---------------------------------------------------------------- Steam install / library

def find_steam_path() -> Optional[Path]:
    """Locate the Steam install folder from the registry, then common locations."""
    candidates: List[str] = []
    try:
        import winreg  # Windows only
        for hive, subkey, value in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    candidates.append(str(winreg.QueryValueEx(key, value)[0]))
            except OSError:
                logger.debug("Steam registry key not found: %s", subkey)
    except ImportError:
        logger.debug("winreg unavailable; skipping registry lookup")
    candidates.extend(_COMMON_STEAM_PATHS)
    for candidate in candidates:
        path = Path(candidate)
        if (path / "steamapps").is_dir():
            return path
    return None


def library_folders(steam_path: Path) -> List[Path]:
    """Every library's ``steamapps`` directory, including the default one."""
    folders = [steam_path / "steamapps"]
    vdf = steam_path / "steamapps" / "libraryfolders.vdf"
    try:
        data = parse_vdf(vdf.read_text(encoding="utf-8", errors="replace"))
    except OSError as err:
        logger.debug("Could not read %s: %s", vdf, err)
        return folders
    section = data.get("libraryfolders") or data.get("LibraryFolders") or {}
    for key, entry in section.items():
        if not key.isdigit():
            continue
        raw = entry.get("path") if isinstance(entry, dict) else entry
        if raw:
            apps = Path(raw) / "steamapps"
            if apps not in folders:
                folders.append(apps)
    return folders


def list_installed(steam_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Installed games as ``[{appid, name, install_dir}]`` sorted by name."""
    steam_path = steam_path or find_steam_path()
    if steam_path is None:
        raise SteamError("Steam does not appear to be installed on this PC.")
    games: Dict[int, Dict[str, Any]] = {}
    for apps_dir in library_folders(steam_path):
        try:
            manifests = sorted(apps_dir.glob("appmanifest_*.acf"))
        except OSError as err:
            logger.debug("Could not scan %s: %s", apps_dir, err)
            continue
        for manifest in manifests:
            try:
                state = parse_vdf(manifest.read_text(encoding="utf-8", errors="replace")).get("AppState", {})
                appid = int(state.get("appid", ""))
            except (OSError, ValueError, AttributeError) as err:
                logger.debug("Skipping unreadable manifest %s: %s", manifest, err)
                continue
            name = state.get("name") or state.get("installdir") or f"App {appid}"
            games[appid] = {"appid": appid, "name": name, "install_dir": state.get("installdir", "")}
    return sorted(games.values(), key=lambda g: g["name"].lower())


def resolve_installed(name_or_appid: str, games: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pick one installed game by appid or name (exact, then unique substring)."""
    needle = str(name_or_appid).strip()
    if not needle:
        raise SteamError("Give a game name or appid.")
    if needle.isdigit():
        for game in games:
            if game["appid"] == int(needle):
                return game
        raise SteamError(f"No installed game has appid {needle}.")
    low = needle.lower()
    exact = [g for g in games if g["name"].lower() == low]
    if len(exact) == 1:
        return exact[0]
    partial = [g for g in games if low in g["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise SteamError(f"No installed game matches '{needle}'.")
    names = ", ".join(g["name"] for g in partial[:5])
    raise SteamError(f"'{needle}' matches several installed games: {names}. Be more specific.")


# ---------------------------------------------------------------- URIs

def _valid_appid(appid: Any) -> int:
    text = str(appid).strip()
    if not text.isdigit() or not 0 < int(text) < 10**10:
        raise SteamError(f"Invalid Steam appid: {appid!r}.")
    return int(text)


def launch_uri(appid: Any) -> str:
    return f"steam://rungameid/{_valid_appid(appid)}"


def store_uri(appid: Any) -> str:
    return f"steam://store/{_valid_appid(appid)}"


def cart_uri() -> str:
    return f"steam://openurl/{CART_URL}"


# ---------------------------------------------------------------- Store search

def _price_text(item: Dict[str, Any]) -> Dict[str, Any]:
    price = item.get("price")
    if not isinstance(price, dict):
        return {"price": "Free or not available", "discount_percent": 0}
    final = price.get("final")
    initial = price.get("initial")
    currency = price.get("currency", "USD")
    out: Dict[str, Any] = {"price": f"{final / 100:.2f} {currency}" if isinstance(final, int) else "Unknown",
                           "discount_percent": 0}
    if isinstance(final, int) and isinstance(initial, int) and initial > final > -1 and initial > 0:
        out["discount_percent"] = round((initial - final) * 100 / initial)
        out["original_price"] = f"{initial / 100:.2f} {currency}"
    return out


def parse_search_results(payload: Dict[str, Any], limit: int = 5) -> List[Dict[str, Any]]:
    results = []
    for item in (payload.get("items") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        try:
            appid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        results.append({"appid": appid, "name": str(item.get("name", "")), **_price_text(item)})
    return results


def search_store(query: str, fetch: Callable[[str], str], limit: int = 5) -> List[Dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        raise SteamError("Give a game name to search for.")
    if len(query) > MAX_QUERY_CHARS:
        raise SteamError(f"Search text is too long (max {MAX_QUERY_CHARS} characters).")
    limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
    try:
        payload = json.loads(fetch(STORE_SEARCH_URL.format(term=quote(query))))
    except json.JSONDecodeError as err:
        raise SteamError("Steam returned an unreadable search response.") from err
    if not isinstance(payload, dict):
        raise SteamError("Steam returned an unexpected search response.")
    return parse_search_results(payload, limit)
