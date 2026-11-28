"""Brave/Chromium tab control over the Chrome DevTools Protocol (Playwright connect_over_cdp).

Playwright's sync API is bound to the thread that started it, so every call runs on one
dedicated worker thread. Disconnecting never closes the user's browser.
"""
import atexit
import os
import re
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from src.config.log import get_logger
from src.config.settings import get_config_value

logger = get_logger(__name__)

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
LAUNCH_HINT = ('Launch Brave with: brave.exe --remote-debugging-port=9222 '
               '--user-data-dir="%USERPROFILE%\\.altron-brave-profile"')
UNTRUSTED_OPEN = "<untrusted_web_content>"
UNTRUSTED_CLOSE = "</untrusted_web_content>"
_BAD_SCHEMES = re.compile(r"^(javascript|data|file|vbscript|about|chrome|brave|blob):", re.I)


_BRAVE_REL = r"BraveSoftware\Brave-Browser\Application\brave.exe"


def find_brave() -> Optional[str]:
    """ALTRON_BRAVE_PATH, then the standard install locations."""
    cands = [get_config_value("ALTRON_BRAVE_PATH", default="")]
    cands += [os.path.join(os.environ.get(v, d), _BRAVE_REL) for v, d in (
        ("ProgramFiles", r"C:\Program Files"), ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local")))]
    return next((c for c in cands if c and os.path.isfile(c)), None)


class BrowserError(Exception):
    """User-facing failure (message is safe to show)."""


def normalize_url(url: str) -> str:
    """Prefix https:// when no scheme; allow only http(s)."""
    url = (url or "").strip()
    if not url:
        raise BrowserError("No URL given")
    if "://" in url:
        if not re.match(r"^https?://", url, re.I):
            raise BrowserError("Only http(s) URLs are allowed")
        return url
    if _BAD_SCHEMES.match(url):
        raise BrowserError("Only http(s) URLs are allowed")
    return "https://" + url


class BrowserService:
    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser-cdp")
        self._pw = None
        self._browser = None
        self._lock = threading.Lock()

    # ---- public API (thread-safe: serialised on the worker thread) ----
    def list_tabs(self) -> List[Dict[str, Any]]:
        return self._run(self._list_tabs)

    def switch_tab(self, query) -> Dict[str, Any]:
        return self._run(self._switch_tab, query)

    def close_tab(self, query) -> Dict[str, Any]:
        return self._run(self._close_tab, query)

    def open_url(self, url: str, new_tab: bool = True) -> Dict[str, Any]:
        return self._run(self._open_url, url, new_tab)

    def read_page(self, query=None, max_chars: int = 4000) -> Dict[str, Any]:
        return self._run(self._read_page, query, max_chars)

    def disconnect(self) -> None:
        """Drop the CDP connection only; the browser keeps running."""
        try:
            self._pool.submit(self._disconnect).result(timeout=10)
        except Exception as e:  # shutdown path: never raise
            logger.debug("browser disconnect failed: %s", e)

    # ---- internals (worker thread only) ----
    def _run(self, fn, *args):
        with self._lock:
            return self._pool.submit(fn, *args).result()

    def _connect(self):
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        self._disconnect()
        url = get_config_value("ALTRON_BROWSER_CDP_URL", default=DEFAULT_CDP_URL) or DEFAULT_CDP_URL
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            try:
                self._browser = self._pw.chromium.connect_over_cdp(url)
            except Exception as e:
                logger.debug("CDP connect to %s failed: %s", url, e)
                if not self._auto_launch(url):
                    raise
                self._browser = self._pw.chromium.connect_over_cdp(url)
        except BrowserError:
            self._disconnect()
            raise
        except Exception as e:
            logger.debug("CDP connect to %s failed: %s", url, e)
            self._disconnect()
            raise BrowserError(f"Cannot reach the browser at {url}. {LAUNCH_HINT}") from e
        return self._browser

    @staticmethod
    def _auto_launch(url: str) -> bool:
        """Start Brave detached on the CDP port and wait for it. False when disabled or non-local."""
        flag = get_config_value("ALTRON_BROWSER_AUTO_LAUNCH", default="true").strip().lower()
        u = urlparse(url)
        if flag in ("0", "false", "no", "off") or u.hostname not in ("127.0.0.1", "localhost"):
            return False
        exe = find_brave()
        if not exe:
            raise BrowserError("Brave was not found. Set ALTRON_BRAVE_PATH in .env to the full path of brave.exe.")
        profile = os.path.join(os.path.expanduser("~"), ".altron-brave-profile")
        subprocess.Popen([exe, f"--remote-debugging-port={u.port or 9222}", f"--user-data-dir={profile}"],
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(url.rstrip("/") + "/json/version", timeout=1).close()  # loopback only
                return True
            except OSError:
                time.sleep(0.3)
        raise BrowserError(f"Launched Brave but it did not answer on {url} within 10s.")

    def _disconnect(self):
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception as e:
                logger.debug("browser %s failed: %s", meth, e)
        self._browser = self._pw = None

    def _pages(self) -> list:
        return [p for c in self._connect().contexts for p in c.pages]

    @staticmethod
    def _info(i: int, page) -> Dict[str, Any]:
        return {"index": i, "title": page.title(), "url": page.url}

    def _list_tabs(self):
        return [self._info(i, p) for i, p in enumerate(self._pages())]

    def _match(self, pages: list, query) -> Dict[str, Any]:
        """{'page', 'index'} for exactly one match, else {'error', 'candidates'?}."""
        q = str(query if query is not None else "").strip()
        if not q:
            return {"error": "Give a tab index, title or URL fragment"}
        if q.isdigit():
            i = int(q)
            if i < len(pages):
                return {"page": pages[i], "index": i}
            return {"error": f"No tab with index {i} ({len(pages)} open)"}
        hits = [(i, p) for i, p in enumerate(pages) if q.lower() in p.title().lower() or q.lower() in p.url.lower()]
        if len(hits) == 1:
            return {"page": hits[0][1], "index": hits[0][0]}
        if not hits:
            return {"error": f"No tab matches '{q}'"}
        return {"error": f"{len(hits)} tabs match '{q}'; pick one by index",
                "candidates": [self._info(i, p) for i, p in hits]}

    def _resolve(self, query) -> Dict[str, Any]:
        r = self._match(self._pages(), query)
        if "page" not in r:
            raise BrowserError(r["error"] + (f": {r['candidates']}" if "candidates" in r else ""))
        return r

    def _switch_tab(self, query):
        r = self._match(self._pages(), query)
        if "page" not in r:
            return {"ok": False, **r}
        r["page"].bring_to_front()
        return {"ok": True, "tab": self._info(r["index"], r["page"])}

    def _close_tab(self, query):
        r = self._match(self._pages(), query)
        if "page" not in r:
            return {"ok": False, **r}
        info = self._info(r["index"], r["page"])
        r["page"].close()
        return {"ok": True, "closed": info}

    def _open_url(self, url, new_tab):
        url = normalize_url(url)
        browser = self._connect()
        if not browser.contexts:
            raise BrowserError("The browser has no open window to use")
        if new_tab:
            page = browser.contexts[0].new_page()
        else:
            pages = self._pages()
            if not pages:
                raise BrowserError("No open tab to navigate")
            page = self._active(pages)
        page.goto(url)
        page.bring_to_front()
        return {"ok": True, "url": page.url, "title": page.title()}

    @staticmethod
    def _active(pages: list):
        # shortcut: "visible" can hold for several windows; first one wins, upgrade if that bites
        for p in pages:
            try:
                if p.evaluate("document.visibilityState") == "visible":
                    return p
            except Exception as e:
                logger.debug("visibility probe failed: %s", e)
        return pages[-1]

    def _read_page(self, query, max_chars):
        pages = self._pages()
        if not pages:
            raise BrowserError("No open tab to read")
        page = self._resolve(query)["page"] if query not in (None, "") else self._active(pages)
        max_chars = max(1, min(int(max_chars or 4000), 20000))
        text = page.inner_text("body")
        truncated = len(text) > max_chars
        return {"ok": True, "title": page.title(), "url": page.url, "truncated": truncated,
                "text": f"{UNTRUSTED_OPEN}\n{text[:max_chars]}\n{UNTRUSTED_CLOSE}"}


_service: Optional[BrowserService] = None
_service_lock = threading.Lock()


def get_browser_service() -> BrowserService:
    global _service
    with _service_lock:
        if _service is None:
            _service = BrowserService()
            atexit.register(_service.disconnect)
        return _service
