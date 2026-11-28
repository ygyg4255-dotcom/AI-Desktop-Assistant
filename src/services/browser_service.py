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
_BLANK = {"", "about:blank", "newtab", "chrome://newtab", "chrome://newtab/", "brave://newtab", "brave://newtab/"}
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


def _hits(pages: list, q: str) -> list:
    """(index, page) pairs whose title or URL contains q (case-insensitive)."""
    q = q.lower()
    return [(i, p) for i, p in enumerate(pages) if q in p.title().lower() or q in p.url.lower()]


def normalize_url(url: str) -> str:
    """Prefix https:// when no scheme; allow only http(s)."""
    url = (url or "").strip()
    if url.lower() in _BLANK:
        return "about:blank"
    if "://" in url:
        if not re.match(r"^https?://", url, re.I):
            raise BrowserError("Only http(s) URLs are allowed")
        return url
    if _BAD_SCHEMES.match(url):
        raise BrowserError("Only http(s) URLs are allowed")
    return "https://" + url


_YT_MARKERS = ("youtube.com/watch", "youtube.com/shorts", "youtu.be")
VIDEO_ACTIONS = ("seek", "forward", "rewind", "play", "pause", "toggle", "mute", "unmute", "speed", "status")
# Returns null when the tab has no <video>, before touching anything.
_VIDEO_JS = """async ([action, val]) => {
  const v = document.querySelector('video'); if (!v) return null;
  const d = isFinite(v.duration) ? v.duration : Infinity;
  const clamp = t => Math.max(0, Math.min(t, d));
  if (action === 'seek') v.currentTime = clamp(val);
  else if (action === 'forward') v.currentTime = clamp(v.currentTime + val);
  else if (action === 'rewind') v.currentTime = clamp(v.currentTime - val);
  else if (action === 'play') await v.play().catch(() => {});
  else if (action === 'pause') v.pause();
  else if (action === 'toggle') { if (v.paused) await v.play().catch(() => {}); else v.pause(); }
  else if (action === 'mute') v.muted = true;
  else if (action === 'unmute') v.muted = false;
  else if (action === 'speed') v.playbackRate = val;
  return {current_time: v.currentTime, duration: isFinite(v.duration) ? v.duration : null,
          paused: v.paused, muted: v.muted, playback_rate: v.playbackRate};
}"""


def parse_timestamp(value) -> float:
    """Seconds from 120, "120", "2:00" or "1:15:30". Raises BrowserError on anything else."""
    bad = BrowserError(f"Invalid time {value!r}: use seconds or MM:SS / HH:MM:SS")
    if isinstance(value, bool) or value is None:
        raise bad
    if isinstance(value, (int, float)):
        secs = float(value)
    else:
        t = str(value).strip()
        if re.fullmatch(r"\d+(\.\d+)?", t):
            secs = float(t)
        elif re.fullmatch(r"\d+:\d{1,2}(:\d{1,2})?", t):
            parts = [int(x) for x in t.split(":")]
            if any(p >= 60 for p in parts[1:]):
                raise bad
            secs = float(sum(p * 60 ** i for i, p in enumerate(reversed(parts))))
        else:
            raise bad
    if not 0 <= secs < float("inf"):  # also rejects NaN
        raise bad
    return secs


def format_time(secs) -> Optional[str]:
    if secs is None:
        return None
    s = int(secs)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


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

    def close_tab(self, query, close_all: bool = False) -> Dict[str, Any]:
        return self._run(self._close_tab, query, close_all)

    def open_url(self, url: str, new_tab: bool = True) -> Dict[str, Any]:
        return self._run(self._open_url, url, new_tab)

    def read_page(self, query=None, max_chars: int = 4000) -> Dict[str, Any]:
        return self._run(self._read_page, query, max_chars)

    def video_control(self, action: str, value=None, tab_query: Optional[str] = None) -> Dict[str, Any]:
        return self._run(self._video_control, action, value, tab_query)

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
        hits = _hits(pages, q)
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

    def _close_tab(self, query, close_all=False):
        pages = self._pages()
        q = str(query if query is not None else "").strip()
        r = self._match(pages, q)
        if close_all and "candidates" in r:  # all matches, held by page reference so closing never shifts indexes
            targets = [(c["index"], pages[c["index"]]) for c in r["candidates"]]
        elif "page" in r:
            targets = [(r["index"], r["page"])]
        else:
            return {"ok": False, **r}
        closed, failed, gone = [], [], []  # closed holds (info, page) until verified
        for i, p in targets:
            info = self._info(i, p)
            try:
                p.close()
            except Exception as e:
                logger.debug("tab close failed: %s", e)
                try:
                    is_gone = p.is_closed()
                except Exception as e2:  # dropped CDP connection: can't confirm, so failed
                    logger.debug("is_closed failed: %s", e2)
                    is_gone = False
                (gone if is_gone else failed).append(info if is_gone else {**info, "error": str(e)})
                continue
            closed.append((info, p))
        live = self._pages()  # verify against the real tab list
        failed += [{**info, "error": "tab still open after close"} for info, p in closed if p in live]
        closed = [info for info, p in closed if p not in live]
        remaining = [] if q.isdigit() else [self._info(i, p) for i, p in _hits(live, q)]
        logger.info("close_tab q=%r close_all=%s closed=%d failed=%d gone=%d remaining=%d",
                    q, close_all, len(closed), len(failed), len(gone), len(remaining))
        out = {"ok": not failed, "closed": closed, "closed_count": len(closed), "failed": failed,
               "already_gone": gone, "remaining_matches": remaining, "open_tabs": len(live)}
        if failed:
            out["error"] = f"{len(failed)} tab(s) failed to close"
        return out

    def _video_control(self, action, value, tab_query):
        action = str(action or "").strip().lower()
        if action not in VIDEO_ACTIONS:
            return {"ok": False, "error": f"Unknown action '{action}'. Use one of: {', '.join(VIDEO_ACTIONS)}"}
        try:
            if action in ("forward", "rewind"):
                arg = parse_timestamp(10 if value in (None, "") else value)
            elif action == "seek":
                arg = parse_timestamp(value)
            elif action == "speed":
                try:
                    arg = min(4.0, max(0.25, float(value)))
                except (TypeError, ValueError):
                    raise BrowserError(f"Invalid speed {value!r}: use a number like 1.5")
                if arg != arg:  # NaN
                    raise BrowserError(f"Invalid speed {value!r}")
            else:
                arg = 0
        except BrowserError as e:
            return {"ok": False, "error": str(e)}
        pages = self._pages()
        if tab_query and str(tab_query).strip():
            cands = [self._resolve(tab_query)["page"]]
        else:  # YouTube watch tabs first, then any other tab that turns out to have a <video>
            yt = [p for p in pages if any(m in p.url.lower() for m in _YT_MARKERS)]
            cands = yt + [p for p in pages if p not in yt]
        for p in cands:
            try:
                st = p.evaluate(_VIDEO_JS, [action, arg])
            except Exception as e:  # e.g. chrome:// pages refuse evaluation
                logger.debug("video evaluate failed: %s", e)
                continue
            if st:
                return {"ok": True, "action": action, "tab_title": p.title(), "url": p.url,
                        "current_time": st["current_time"], "current_time_formatted": format_time(st["current_time"]),
                        "duration": st["duration"], "duration_formatted": format_time(st["duration"]),
                        "paused": st["paused"], "muted": st["muted"], "playback_rate": st["playback_rate"]}
        return {"ok": False, "error": "No <video> element found in " +
                ("that tab" if tab_query and str(tab_query).strip() else "any open tab")}

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
        if url != "about:blank":
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
