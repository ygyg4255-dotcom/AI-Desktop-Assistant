"""Cache-aware sticky tool routing.

CORE tools (everything not in a pack) are always sent. A domain pack is added when the user's
message matches its keywords or the model calls ``load_tool_pack``, and then stays for the
session (until ``reset``), so the ``tools`` array only ever grows and the Anthropic prompt
cache is not invalidated by flapping.
"""
import re
import threading
from typing import Any, Dict, List

from src.config.settings import get_config_value

# Fixed order: pack order and tool order inside a pack are part of the cached prefix.
PACKS: Dict[str, tuple] = {
    "browser": ("browser_list_tabs", "browser_read_page", "browser_open_url",
                "browser_switch_tab", "browser_close_tab", "browser_open", "open_url"),
    "email_messaging": ("gmail_list_messages", "gmail_read_message", "gmail_send_email",
                        "whatsapp_send", "whatsapp_read", "send_to_telegram", "telegram_send_file"),
    "office_docs": ("deckcraft_create", "deckcraft_read", "deckcraft_edit",
                    "wordcraft_create", "wordcraft_read", "wordcraft_edit",
                    "gridcraft_create", "gridcraft_read", "gridcraft_edit",
                    "pdfcraft_read", "pdfcraft_merge", "pdfcraft_split", "create_and_send_pdf"),
    "gaming_media": ("steam_list_installed", "steam_launch_game", "steam_search_store",
                     "steam_open_store_page", "steam_add_to_cart", "play_music",
                     "youtube_play", "youtube_play_and_summarize"),
    "dev_coding": ("claude_code_run", "code_edit", "terminal_execute"),
}

_KEYWORDS = {
    "browser": r"\b(brave|browser|tabs?|url|website|https?|www)\b|متصفح|تبويب|تبويبة|علامة تبويب",
    "email_messaging": r"\b(gmail|e-?mail|inbox|whatsapp|telegram)\b|بريد|إيميل|ايميل|واتس|تيليجرام|تلغرام",
    "office_docs": r"\b(word|excel|powerpoint|pdf|docx|xlsx|pptx|spreadsheet|slides?)\b|ورد|اكسل|إكسل|بوربوينت",
    "gaming_media": r"\b(steam|spotify|youtube|game|games)\b|لعبة|ألعاب|سبوتيفاي|يوتيوب",
    "dev_coding": r"\b(code|coding|terminal|script|refactor)\b|كود|ترمنال|ترمينال|برمجة",
}
_KEYWORD_RES = {k: re.compile(v, re.IGNORECASE) for k, v in _KEYWORDS.items()}

_PACKED = {n for names in PACKS.values() for n in names}

# Read-only SAFE tools that may run concurrently. gmail_* / browser_* are left out on purpose:
# their clients (httplib2, Playwright) are not thread-safe.
PARALLEL_SAFE = frozenset({
    "file_search", "file_list", "process_list", "system_info", "usage_summary", "web_fetch",
    "web_search", "list_reminders", "list_scheduled_tasks", "todo_list", "note_list", "note_read",
    "steam_list_installed", "steam_search_store",
    "deckcraft_read", "wordcraft_read", "gridcraft_read", "pdfcraft_read",
})


def dynamic_tools_enabled() -> bool:
    return get_config_value("ALTRON_DYNAMIC_TOOLS", default="true").strip().lower() not in ("0", "false", "no", "off")


class ToolRouter:
    def __init__(self, registry):
        self._registry = registry
        self._active: set = set()
        self._lock = threading.Lock()

    @property
    def active_packs(self) -> List[str]:
        return [p for p in PACKS if p in self._active]

    def activate(self, pack: str) -> bool:
        if pack not in PACKS:
            return False
        with self._lock:
            self._active.add(pack)
        return True

    def observe(self, user_message: str) -> None:
        """Pre-activate every pack whose keywords appear in the user's message."""
        for pack, rx in _KEYWORD_RES.items():
            if rx.search(user_message or ""):
                self.activate(pack)

    def reset(self) -> None:
        with self._lock:
            self._active.clear()

    def schema(self) -> List[Dict[str, Any]]:
        if not dynamic_tools_enabled():
            return self._registry.get_tools_schema()
        out = [t.to_api_schema() for t in self._registry.list_tools() if t.name not in _PACKED]
        for pack in self.active_packs:
            out += [t.to_api_schema() for n in PACKS[pack] if (t := self._registry.get(n))]
        return out
