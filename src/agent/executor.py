"""Agent Executor - Core Agent Loop for AI Desktop Assistant.

Implements a Claude Code-inspired agent loop:
User Prompt -> Agent analyzes -> Selects Tool -> Executes -> Verifies Output -> Final Response
"""
import json
import time
import re
import difflib
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable, Union
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import Future

from src.agent.tools import ToolRegistry, ToolResult, create_default_registry
from src.agent.permissions import PermissionGate, PermissionLevel
from src.services.ai_client import AIClient, Message
from src.storage.db import Database


class AgentState(Enum):
    """Agent execution state."""
    IDLE = "idle"
    THINKING = "thinking"
    AWAITING_PERMISSION = "awaiting_permission"
    EXECUTING = "executing"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class AgentStep:
    """Represents a single step in the agent loop."""
    step_type: str  # "thinking", "tool_use", "tool_result", "response"
    content: str = ""
    tool_name: Optional[str] = None
    tool_input: Dict[str, Any] = field(default_factory=dict)
    tool_result: Optional[ToolResult] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class AgentResponse:
    """Final response from the agent."""
    content: str
    steps: List[AgentStep] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None


class PermissionRequest:
    """Represents a pending permission request."""

    def __init__(self, tool_name: str, tool_input: Dict[str, Any], message: str, is_dangerous: bool = False):
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.message = message
        self.is_dangerous = is_dangerous
        self._response: Optional[bool] = None
        self._session_allow: bool = False
        self._event = threading.Event()

    def approve(self, session_allow: bool = False) -> None:
        """Approve the permission request."""
        self._session_allow = session_allow
        self._response = True
        self._event.set()

    def reject(self) -> None:
        """Reject the permission request."""
        self._response = False
        self._event.set()

    def wait(self, timeout: Optional[float] = None) -> Optional[bool]:
        """Wait for the user's response."""
        self._event.wait(timeout)
        return self._response

    @property
    def session_allow(self) -> bool:
        """Whether permission was granted for the full session."""
        return self._session_allow

    @property
    def response(self) -> Optional[bool]:
        """Get the response without waiting."""
        return self._response


def match_local_intent(prompt: str) -> Optional[Dict[str, Any]]:
    """Match direct system control and YouTube commands to bypass LLM latency."""
    if not prompt:
        return None

    p = prompt.strip().lower()
    clean_p = re.sub(r'[\.\,\!\?؟]+$', '', p).strip()
    is_arabic = any('؀' <= char <= 'ۿ' for char in clean_p)

    # Normalize Arabic/Eastern digits to standard 0-9
    clean_p_norm = clean_p
    for ar_digit, en_digit in zip("٠١٢٣٤٥٦٧٨٩", "0123456789"):
        clean_p_norm = clean_p_norm.replace(ar_digit, en_digit)

    # Arabic text normalization for fuzzy matching
    def _normalize_arabic(text: str) -> str:
        text = re.sub(r'[ً-ٰٟ]', '', text)  # Tashkeel / diacritics
        text = re.sub(r'[إأآا]', 'ا', text)               # Normalize Alef variants
        text = re.sub(r'ة', 'ه', text)                     # Normalize Teh Marbuta
        text = re.sub(r'ى', 'ي', text)                     # Normalize Alef Maksura
        return text

    norm_ar = _normalize_arabic(clean_p_norm)
    tokens = clean_p_norm.split()

    def _fuzzy_token(token: str, target: str, min_ratio: float = 0.72) -> bool:
        if token == target:
            return True
        return difflib.SequenceMatcher(None, token, target).ratio() >= min_ratio

    # 0. Desktop Icon Positioning Intent (English & Arabic)
    ar_pos_map = {
        "وسط": "center", "الوسط": "center", "للوسط": "center", "منتصف": "center", "المنتصف": "center", "للمنتصف": "center",
        "مركز": "center", "المركز": "center", "للمركز": "center",
        "يمين": "right", "اليمين": "right", "لليمين": "right",
        "يسار": "left", "اليسار": "left", "لليسار": "left", "شمال": "left", "الشمال": "left", "للشمال": "left",
        "اعلى": "top", "أعلى": "top", "الاعلى": "top", "الأعلى": "top", "للاعلى": "top", "للأعلى": "top", "فوق": "top", "لفوق": "top",
        "اسفل": "bottom", "أسفل": "bottom", "الاسفل": "bottom", "الأسفل": "bottom", "للاسفل": "bottom", "للأسفل": "bottom", "تحت": "bottom", "لتحت": "bottom",
        "اعلى اليمين": "top_right", "أعلى اليمين": "top_right", "فوق يمين": "top_right", "لأعلى اليمين": "top_right",
        "اعلى اليسار": "top_left", "أعلى اليسار": "top_left", "فوق يسار": "top_left", "لأعلى اليسار": "top_left",
        "اسفل اليمين": "bottom_right", "أسفل اليمين": "bottom_right", "تحت يمين": "bottom_right", "لأسفل اليمين": "bottom_right",
        "اسفل اليسار": "bottom_left", "أسفل اليسار": "bottom_left", "تحت يسار": "bottom_left", "لأسفل اليسار": "bottom_left"
    }
    pos_keys = "|".join(sorted(ar_pos_map.keys(), key=len, reverse=True))

    ar_desktop_pat = re.compile(
        rf'^(?:حرك|انقل|ضع|ودي|جيب)\s+(?:ملف|ايقونة|أيقونة|عنصر)?\s*(.+?)\s+(?:إلى|الى|في|لـ|ل)?\s*({pos_keys})(?:\s+(?:من\s+)?الشاشة|\s+(?:من\s+)?سطح المكتب)?$',
        re.IGNORECASE
    )
    en_desktop_pat = re.compile(
        r'^(?:move|position|place|relocate|put|set)\s+(?:desktop\s+)?(?:icon|file|item)?\s*(.+?)\s+(?:to\s+(?:the\s+)?|at\s+(?:the\s+)?|in\s+(?:the\s+)?)(center|middle|centre|top|bottom|left|right|top[ -_]left|top[ -_]right|bottom[ -_]left|bottom[ -_]right)(?:\s+(?:of\s+)?(?:the\s+)?(?:screen|desktop))?$',
        re.IGNORECASE
    )

    m_en_dt = en_desktop_pat.match(clean_p)
    m_ar_dt = ar_desktop_pat.match(clean_p)
    if m_en_dt:
        dt_target = m_en_dt.group(1).strip().strip('"\'')
        dt_pos = m_en_dt.group(2).strip().lower().replace(" ", "_").replace("-", "_")
        if dt_pos in ("middle", "centre"):
            dt_pos = "center"
        return {
            "tool_name": "desktop_icon_position",
            "tool_input": {"target": dt_target, "position": dt_pos},
            "confirmation": f"✅ Successfully moved '{dt_target}' icon to {dt_pos.replace('_', ' ')}."
        }
    elif m_ar_dt:
        dt_target = m_ar_dt.group(1).strip().strip('"\'')
        dt_pos_raw = m_ar_dt.group(2).strip()
        dt_pos = ar_pos_map.get(dt_pos_raw, "center")
        pos_ar_desc = {
            "center": "وسط الشاشة",
            "top": "أعلى الشاشة",
            "bottom": "أسفل الشاشة",
            "left": "يسار الشاشة",
            "right": "يمين الشاشة",
            "top_right": "أعلى يمين الشاشة",
            "top_left": "أعلى يسار الشاشة",
            "bottom_right": "أسفل يمين الشاشة",
            "bottom_left": "أسفل يسار الشاشة"
        }.get(dt_pos, "وسط الشاشة")
        return {
            "tool_name": "desktop_icon_position",
            "tool_input": {"target": dt_target, "position": dt_pos},
            "confirmation": f"✅ تم تحريك أيقونة '{dt_target}' إلى {pos_ar_desc} بنجاح."
        }

    # 1. YouTube Play (Priority if explicit YouTube search/play query)
    yt_patterns = [
        # Arabic specific
        r'^(?:افتح\s+اليوتيوب\s+وشغل|افتح\s+يوتيوب\s+وشغل)\s+(.+)$',
        r'^(?:شغل|افتح|ابحث عن)\s+(.+?)\s+(?:على\s+اليوتيوب|على\s+يوتيوب|في\s+اليوتيوب|في\s+يوتيوب|يوتيوب)$',
        r'^(?:شغل|افتح)\s+(?:على\s+اليوتيوب|على\s+يوتيوب|في\s+اليوتيوب|في\s+يوتيوب|يوتيوب)\s+(.+)$',
        r'^(?:شغل\s+اغنية|شغل\s+أغنية|شغل\s+فيديو|شغل\s+كليب|شغل\s+موسيقى)\s+(.+)$',
        # English specific
        r'^(?:open\s+youtube\s+and\s+play)\s+(.+)$',
        r'^(?:play|open)\s+(.+?)\s+(?:on\s+youtube|in\s+youtube|youtube)$',
        r'^(?:youtube\s+play|play\s+on\s+youtube|play\s+in\s+youtube)\s+(.+)$',
        r'^(?:play\s+song|play\s+video|play\s+music)\s+(.+)$',
    ]
    for pattern in yt_patterns:
        match = re.match(pattern, clean_p, re.IGNORECASE)
        if match:
            query = match.group(1).strip().strip('"\'')
            if query:
                msg = f"✅ تم تشغيل '{query}' على يوتيوب." if is_arabic else f"✅ Playing '{query}' on YouTube."
                return {
                    "tool_name": "youtube_play",
                    "tool_input": {"query": query},
                    "confirmation": msg
                }

    # 2. Windows Settings with Typo Tolerance (e.g. "open srtting", "open seting", "settings", "افتح الاعدادات")
    is_settings = False
    open_verbs = {"open", "launch", "show", "goto", "start", "opne", "oepn", "opn"}
    settings_words = {"settings", "setting", "seting", "setings", "srtting", "srttings", "settngs", "sttings", "configuration", "config"}

    if clean_p_norm in settings_words or any(_fuzzy_token(clean_p_norm, target, 0.72) for target in ["settings", "setting"]):
        is_settings = True
    elif len(tokens) >= 2:
        has_open = any(t in open_verbs or _fuzzy_token(t, "open", 0.75) for t in tokens)
        has_setting = any(t in settings_words or _fuzzy_token(t, "settings", 0.72) or _fuzzy_token(t, "setting", 0.75) for t in tokens)
        if has_open and has_setting:
            is_settings = True

    # Arabic Settings
    if not is_settings:
        ar_settings_roots = {"اعدادات", "الاعدادات", "ضبط", "الضبط"}
        if norm_ar in ar_settings_roots or any(r in norm_ar for r in ["اعدادات الويندوز", "اعدادات النظام", "اعدادات الجهاز"]):
            is_settings = True
        elif any(root in norm_ar for root in ["اعدادات", "ضبط"]):
            ar_verbs = ["افتح", "فتح", "شغل", "عرض", "وريني", "ادخل"]
            if any(v in norm_ar for v in ar_verbs) or len(norm_ar.split()) <= 2:
                is_settings = True

    if is_settings:
        msg = "✅ تم فتح إعدادات النظام." if is_arabic else "✅ Opened Windows Settings."
        return {
            "tool_name": "system_control",
            "tool_input": {"action": "open_settings"},
            "confirmation": msg
        }

    # 3. Numeric Volume & Brightness Settings with Fuzzy Matching (e.g. "volum 33", "صوت 33", "brightness 50")
    if not ("youtube" in clean_p_norm or "يوتيوب" in clean_p_norm):
        m_num = re.search(r'(\d{1,3})', clean_p_norm)
        if m_num:
            val = max(0, min(100, int(m_num.group(1))))

            # Check for brightness first
            is_bright = (
                any(t in {"brightness", "brightnes", "screen", "display"} for t in tokens) or
                any(_fuzzy_token(t, "brightness", 0.75) for t in tokens) or
                any(w in norm_ar for w in {"سطوع", "السطوع", "اضاءة", "الاضاءة", "اضاءه", "الاضاءه", "نور"})
            )
            if is_bright:
                msg = f"✅ تم ضبط السطوع على {val}%." if is_arabic else f"✅ Brightness set to {val}%."
                return {
                    "tool_name": "system_control",
                    "tool_input": {"action": "set_brightness", "value": val},
                    "confirmation": msg
                }

            # Check for volume (e.g. "volum 33", "volume 33", "vol 33", "sound 33", "صوت 33", "الصوت 33")
            is_vol = (
                any(t in {"vol", "volume", "volum", "voulme", "volue", "volumn", "sound", "audio"} for t in tokens) or
                any(_fuzzy_token(t, "volume", 0.72) for t in tokens) or
                any(_fuzzy_token(t, "sound", 0.75) for t in tokens) or
                ("صوت" in norm_ar)
            )
            if is_vol:
                msg = f"✅ تم ضبط مستوى الصوت على {val}%." if is_arabic else f"✅ Volume set to {val}%."
                return {
                    "tool_name": "system_control",
                    "tool_input": {"action": "set_volume", "value": val},
                    "confirmation": msg
                }

    # 4. Volume Up
    vol_up_en = {"volume up", "increase volume", "raise volume", "turn up volume", "louder", "sound up", "volume +", "vol up", "volum up"}
    vol_up_tokens = {"up", "+"}
    is_vol_up = (
        clean_p_norm in vol_up_en or
        (any(t in vol_up_tokens for t in tokens) and any(t in {"vol", "volume", "volum", "sound", "audio"} or _fuzzy_token(t, "volume", 0.72) for t in tokens)) or
        ("صوت" in norm_ar and any(w in norm_ar for w in {"ارفع", "علي", "رفع", "زود", "تعلي", "اعلى", "زياده", "زيادة"}))
    )
    if is_vol_up:
        msg = "✅ تم رفع مستوى الصوت." if is_arabic else "✅ Volume increased."
        return {
            "tool_name": "system_control",
            "tool_input": {"action": "volume_up"},
            "confirmation": msg
        }

    # 5. Volume Down
    vol_down_en = {"volume down", "decrease volume", "lower volume", "turn down volume", "quieter", "sound down", "volume -", "vol down", "volum down"}
    vol_down_tokens = {"down", "-"}
    is_vol_down = (
        clean_p_norm in vol_down_en or
        (any(t in vol_down_tokens for t in tokens) and any(t in {"vol", "volume", "volum", "sound", "audio"} or _fuzzy_token(t, "volume", 0.72) for t in tokens)) or
        ("صوت" in norm_ar and any(w in norm_ar for w in {"خفض", "اخفض", "وطي", "نزل", "قلل", "توطيه", "توطية", "اقل"}))
    )
    if is_vol_down:
        msg = "✅ تم خفض مستوى الصوت." if is_arabic else "✅ Volume decreased."
        return {
            "tool_name": "system_control",
            "tool_input": {"action": "volume_down"},
            "confirmation": msg
        }

    # 6. Volume Mute
    vol_mute_en = {"mute", "unmute", "mute volume", "mute sound", "mute audio", "silence"}
    is_vol_mute = (
        clean_p_norm in vol_mute_en or
        any(_fuzzy_token(t, "mute", 0.75) for t in tokens) or
        any(w in norm_ar for w in {"كتم", "صامت", "اخرس", "الغاء الكتم", "فك الكتم"})
    )
    if is_vol_mute:
        msg = "✅ تم كتم/إلغاء كتم الصوت." if is_arabic else "✅ Volume muted/unmuted."
        return {
            "tool_name": "system_control",
            "tool_input": {"action": "volume_mute"},
            "confirmation": msg
        }

    # 7. Brightness Up
    bright_up_en = {"brightness up", "increase brightness", "raise brightness", "brighter", "screen brighter", "brightness +"}
    is_bright_up = (
        clean_p_norm in bright_up_en or
        ("up" in tokens and any(t in {"brightness", "brightnes", "screen"} or _fuzzy_token(t, "brightness", 0.75) for t in tokens)) or
        (("سطوع" in norm_ar or "اضاءة" in norm_ar or "اضاءه" in norm_ar) and any(w in norm_ar for w in {"ارفع", "علي", "رفع", "زود", "اعلى", "زياده", "زيادة"}))
    )
    if is_bright_up:
        msg = "✅ تم رفع السطوع." if is_arabic else "✅ Brightness increased."
        return {
            "tool_name": "system_control",
            "tool_input": {"action": "brightness_up"},
            "confirmation": msg
        }

    # 8. Brightness Down
    bright_down_en = {"brightness down", "decrease brightness", "lower brightness", "dimmer", "dim screen", "brightness -"}
    is_bright_down = (
        clean_p_norm in bright_down_en or
        ("down" in tokens and any(t in {"brightness", "brightnes", "screen"} or _fuzzy_token(t, "brightness", 0.75) for t in tokens)) or
        (("سطوع" in norm_ar or "اضاءة" in norm_ar or "اضاءه" in norm_ar) and any(w in norm_ar for w in {"خفض", "اخفض", "وطي", "نزل", "قلل", "اقل"}))
    )
    if is_bright_down:
        msg = "✅ تم خفض السطوع." if is_arabic else "✅ Brightness decreased."
        return {
            "tool_name": "system_control",
            "tool_input": {"action": "brightness_down"},
            "confirmation": msg
        }

    return None


def clean_tool_confirmation(
    content: str,
    last_tool_name: str,
    last_tool_input: Dict[str, Any],
    last_result: Optional[ToolResult],
    user_message: str
) -> str:
    """Ensure raw [Tool Result] JSON is never displayed, returning a natural confirmation message."""
    trimmed = (content or "").strip()
    is_raw_output = (
        not trimmed
        or "[Tool Result]" in trimmed
        or (trimmed.startswith("{") and trimmed.endswith("}"))
        or trimmed.startswith("```json")
        or ('"status":' in trimmed and ('"message":' in trimmed or '"copied_files":' in trimmed or '"destination":' in trimmed))
    )

    # If the LLM already returned a natural, non-raw response, keep it
    if not is_raw_output and len(trimmed) > 0:
        return trimmed

    is_arabic = any('؀' <= char <= 'ۿ' for char in user_message)

    if not last_result or not last_result.success:
        err = last_result.error if last_result else "Unknown error"
        return f"❌ تعذر تنفيذ العملية: {err}" if is_arabic else f"❌ Operation failed: {err}"

    output = last_result.output
    out_dict = output if isinstance(output, dict) else {}

    # Extract any count mentioned in user_message, input, or output
    count = None
    if isinstance(output, dict):
        for key in ("copied_files", "count", "total", "copied", "moved", "files"):
            val = output.get(key)
            if isinstance(val, (int, float)):
                count = int(val)
                break

    if count is None:
        # Check numbers in user message like "11400" or "11,400"
        m = re.search(r'\b(\d{1,3}(?:,\d{3})+|\d+)\b', user_message)
        if m:
            try:
                count = int(m.group(1).replace(",", ""))
            except ValueError:
                pass

    count_str_ar = f"{count:,} " if count else ""
    count_str_en = f"{count:,} " if count else ""

    if last_tool_name == "file_copy":
        dst = out_dict.get("destination") or last_tool_input.get("destination", "")
        dst_name = Path(dst).name if dst else ("المجلد المحدد" if is_arabic else "destination folder")
        if is_arabic:
            unit = "ملف" if count else "الملفات"
            return f"✅ تم نسخ {count_str_ar}{unit} إلى مجلد {dst_name} بنجاح!"
        else:
            unit = "file(s)" if count else "files"
            return f"✅ Successfully copied {count_str_en}{unit} to '{dst_name}'!"

    elif last_tool_name == "file_move":
        dst = out_dict.get("destination") or last_tool_input.get("destination", "")
        dst_name = Path(dst).name if dst else ("المجلد المحدد" if is_arabic else "destination folder")
        if is_arabic:
            unit = "ملف" if count else "الملفات"
            return f"✅ تم نقل {count_str_ar}{unit} إلى مجلد {dst_name} بنجاح!"
        else:
            unit = "file(s)" if count else "files"
            return f"✅ Successfully moved {count_str_en}{unit} to '{dst_name}'!"

    elif last_tool_name == "file_delete":
        target = out_dict.get("deleted") or last_tool_input.get("path", "")
        target_name = Path(target).name if target else ""
        if is_arabic:
            return f"✅ تم حذف '{target_name or 'العنصر'}' بنجاح."
        else:
            return f"✅ Successfully deleted '{target_name or 'item'}'."

    elif last_tool_name == "file_write":
        path = out_dict.get("path") or last_tool_input.get("path", "")
        file_name = Path(path).name if path else ""
        if is_arabic:
            return f"✅ تم حفظ الملف '{file_name or 'الجديد'}' بنجاح."
        else:
            return f"✅ Successfully saved file '{file_name or 'file'}'."

    elif last_tool_name == "terminal_execute":
        cmd = str(last_tool_input.get("command", ""))
        if any(w in cmd.lower() for w in ("copy", "robocopy", "cp")):
            if is_arabic:
                unit = "ملف" if count else "الملفات"
                return f"✅ تم نسخ {count_str_ar}{unit} بنجاح!"
            else:
                unit = "file(s)" if count else "files"
                return f"✅ Successfully copied {count_str_en}{unit}!"
        return "✅ تم تنفيذ الأمر بنجاح." if is_arabic else "✅ Command executed successfully."

    elif last_tool_name == "app_open":
        app = out_dict.get("app") or last_tool_input.get("app_name") or last_tool_input.get("path", "")
        app_name = Path(app).name if app else ""
        if is_arabic:
            return f"✅ تم تشغيل {app_name or 'التطبيق'}."
        else:
            return f"✅ Opened {app_name or 'application'}."

    elif last_tool_name == "desktop_icon_position":
        target = last_tool_input.get("target", "")
        pos = str(last_tool_input.get("position", "center")).lower().replace("-", "_").replace(" ", "_")
        pos_ar_map = {
            "center": "وسط الشاشة",
            "middle": "وسط الشاشة",
            "centre": "وسط الشاشة",
            "top": "أعلى الشاشة",
            "bottom": "أسفل الشاشة",
            "left": "يسار الشاشة",
            "right": "يمين الشاشة",
            "top_left": "أعلى يسار الشاشة",
            "top_right": "أعلى يمين الشاشة",
            "bottom_left": "أسفل يسار الشاشة",
            "bottom_right": "أسفل يمين الشاشة",
        }
        pos_ar = pos_ar_map.get(pos, "الموضع المحدد")
        pos_en = pos.replace("_", " ").title()
        if is_arabic:
            return f"✅ تم تحريك أيقونة '{target}' إلى {pos_ar} بنجاح!"
        else:
            return f"✅ Successfully moved '{target}' icon to {pos_en} on the desktop!"

    # Generic dictionary message or summary
    msg = out_dict.get("message") or out_dict.get("summary")
    if msg and isinstance(msg, str):
        if is_arabic and not any('؀' <= char <= 'ۿ' for char in msg):
            return "✅ تم إكمال العملية بنجاح."
        return f"✅ {msg}"

    if is_arabic:
        return "✅ تم تنفيذ العملية المطلوبة بنجاح!"
    else:
        return "✅ Successfully completed the requested action!"


class AgentExecutor:
    """Core agent execution engine."""

    SYSTEM_PROMPT = """You are an AI Desktop Assistant with access to system tools.

You help users interact with their Windows computer through natural language.

IMPORTANT RULES:
1. When asked to adjust volume (volume_up, volume_down, volume_mute, set_volume), screen brightness (brightness_up, brightness_down, set_brightness), or open Windows settings, ALWAYS use the `system_control` tool for instant execution. NEVER use terminal_execute or PowerShell for volume or brightness.
2. When asked to play any song, music, or video on YouTube, ALWAYS use the `youtube_play` tool with the search query to directly launch and play the video.
3. When asked to create, write, or generate a PDF report or send documents to the user's phone, use `create_and_send_pdf` or `send_to_telegram`.
4. When asked to visually move or position desktop icons or files on the Windows Desktop screen (e.g., to center, top, bottom, left, right, or specific coordinates), ALWAYS use the `desktop_icon_position` tool.
5. Analyze the user's request carefully before using any tool.
6. Only use tools when necessary - simple questions don't require tools.
7. Always explain what you're doing before using a tool.
8. For file operations, always verify paths exist before reading.
9. For terminal commands, be cautious and explain the command.
10. If a tool fails, explain the error and suggest alternatives.
11. After executing any tool, ALWAYS provide a friendly, natural, and concise confirmation message in the user's language (Arabic or English), e.g., '✅ تم نسخ 11,400 ملف إلى مجلد her بنجاح!'. NEVER output raw JSON, '[Tool Result]', or technical data dumps to the user.

Available tools are provided in the tool definitions. Use them responsibly."""

    def __init__(
        self,
        ai_client: AIClient,
        database: Optional[Database] = None,
        registry: Optional[ToolRegistry] = None,
        gate: Optional[PermissionGate] = None
    ):
        self._ai_client = ai_client
        self._database = database
        self._registry = registry or create_default_registry()
        self._gate = gate or PermissionGate()
        self._state = AgentState.IDLE
        self._conversation_id: Optional[int] = None
        self._messages: List[Message] = []
        self._steps: List[AgentStep] = []
        self._pending_permission: Optional[PermissionRequest] = None
        self._cancel_flag = False
        self._max_iterations = 10

    @property
    def state(self) -> AgentState:
        """Get current agent state."""
        return self._state

    @property
    def pending_permission(self) -> Optional[PermissionRequest]:
        """Get the pending permission request, if any."""
        return self._pending_permission

    @property
    def tools_schema(self) -> List[Dict[str, Any]]:
        """Get the tools schema for API calls."""
        return self._registry.get_tools_schema()

    @property
    def conversation_id(self) -> Optional[int]:
        """Get active conversation id."""
        return self._conversation_id

    @property
    def gate(self) -> PermissionGate:
        """Get the permission gate."""
        return self._gate

    def start_conversation(self, title: str = "") -> int:
        """Start a new conversation."""
        if self._database:
            self._conversation_id = self._database.create_conversation(title)
        else:
            self._conversation_id = 1
        self._messages = []
        self._steps = []
        return self._conversation_id

    def load_conversation(self, conversation_id: int) -> None:
        """Load an existing conversation."""
        self._conversation_id = conversation_id
        self._messages = []

        if self._database:
            db_messages = self._database.get_messages(conversation_id)
            for msg in db_messages:
                self._messages.append(Message(
                    role=msg.role,
                    content=msg.content,
                    timestamp=time.time()
                ))

    def _build_messages_with_tools(self, user_message: str) -> List[Dict[str, Any]]:
        """Build messages including tool definitions and history."""
        messages = []

        for msg in self._messages:
            messages.append(msg.to_api_format())

        if user_message:
            messages.append({"role": "user", "content": user_message})

        return messages

    def _parse_tool_use(self, response_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract tool use blocks from API response."""
        tool_uses = []
        content = response_data.get("content", [])

        for block in content:
            if block.get("type") == "tool_use":
                tool_uses.append({
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input", {})
                })

        return tool_uses

    def _execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> ToolResult:
        """Execute a tool by name."""
        tool = self._registry.get(tool_name)
        if not tool:
            return ToolResult(
                success=False,
                output=None,
                error=f"Unknown tool: {tool_name}"
            )

        return tool.handler(tool_input)

    def _execute_tool_with_permission(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        permission_callback: Optional[Callable[[PermissionRequest], None]] = None
    ) -> ToolResult:
        """Execute a tool, requesting permission if needed."""
        tool = self._registry.get(tool_name)
        if not tool:
            return ToolResult(
                success=False,
                output=None,
                error=f"Unknown tool: {tool_name}"
            )

        # Check session permission
        if self._gate.get_permission_level(tool_name) == PermissionLevel.ALLOW:
            exec_input = dict(tool_input)
            exec_input["_approved"] = True
            return tool.handler(exec_input)
        elif self._gate.get_permission_level(tool_name) == PermissionLevel.DENY:
            return ToolResult(
                success=False,
                output=None,
                error=f"Tool '{tool_name}' is blocked by session settings"
            )

        # Check database for saved permission
        if self._database:
            saved_permission = self._database.get_tool_permission(tool_name)
            if saved_permission == "allow":
                exec_input = dict(tool_input)
                exec_input["_approved"] = True
                return tool.handler(exec_input)
            elif saved_permission == "deny":
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Tool '{tool_name}' is blocked by database permission settings"
                )

        # Tool requires permission
        if tool.requires_permission:
            result = tool.handler(tool_input)

            if result.requires_permission:
                self._state = AgentState.AWAITING_PERMISSION
                permission = PermissionRequest(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    message=result.permission_message,
                    is_dangerous=result.is_dangerous
                )
                self._pending_permission = permission

                if permission_callback:
                    permission_callback(permission)

                return result

        return tool.handler(tool_input)

    def process(
        self,
        user_message: str,
        on_step: Optional[Callable[[AgentStep], None]] = None,
        on_permission: Optional[Callable[[PermissionRequest], None]] = None,
        on_complete: Optional[Callable[[AgentResponse], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None
    ) -> Future:
        """Process a user message asynchronously."""
        future: Future = Future()
        self._cancel_flag = False
        self._state = AgentState.THINKING
        self._steps = []

        def _worker():
            try:
                response = self._process_sync(
                    user_message,
                    on_step=on_step,
                    on_permission=on_permission
                )
                if not future.done():
                    future.set_result(response)
                if on_complete:
                    on_complete(response)
            except Exception as e:
                self._state = AgentState.ERROR
                if not future.done():
                    future.set_exception(e)
                if on_error:
                    try:
                        on_error(e)
                    except Exception:
                        pass
            finally:
                # Always reset state to IDLE so subsequent messages can be processed
                self._state = AgentState.IDLE

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        return future

    def _process_sync(
        self,
        user_message: str,
        on_step: Optional[Callable[[AgentStep], None]] = None,
        on_permission: Optional[Callable[[PermissionRequest], None]] = None
    ) -> AgentResponse:
        """Synchronous processing logic with fast local intent bypass."""
        tools_used = []

        user_msg = Message(role="user", content=user_message, timestamp=time.time())
        self._messages.append(user_msg)

        if self._database and self._conversation_id:
            self._database.add_message(
                self._conversation_id,
                "user",
                user_message
            )

        # Fast Local Intent Router (Bypass LLM for instant system controls and YouTube playback)
        local_intent = match_local_intent(user_message)
        if local_intent:
            tool_name = local_intent["tool_name"]
            tool_input = local_intent["tool_input"]
            confirmation = local_intent["confirmation"]

            self._state = AgentState.EXECUTING
            tool_step = AgentStep(
                step_type="tool_use",
                content=f"Using tool: {tool_name}",
                tool_name=tool_name,
                tool_input=tool_input
            )
            self._steps.append(tool_step)
            if on_step:
                on_step(tool_step)

            result = self._execute_tool(tool_name, tool_input)

            result_step = AgentStep(
                step_type="tool_result",
                content=f"Tool result: {result.output if result.success else result.error}",
                tool_name=tool_name,
                tool_result=result
            )
            self._steps.append(result_step)
            if on_step:
                on_step(result_step)

            tools_used.append(tool_name)
            final_content = confirmation if result.success else f"Action failed: {result.error}"

            assistant_msg = Message(role="assistant", content=final_content, timestamp=time.time())
            self._messages.append(assistant_msg)

            if self._database and self._conversation_id:
                self._database.add_message(
                    self._conversation_id,
                    "assistant",
                    final_content
                )

            self._state = AgentState.IDLE
            return AgentResponse(
                content=final_content,
                steps=self._steps,
                tools_used=tools_used,
                success=result.success,
                error=result.error if not result.success else None
            )

        iteration = 0
        final_content = ""
        last_tool_name = ""
        last_tool_input: Dict[str, Any] = {}
        last_tool_result: Optional[ToolResult] = None

        while iteration < self._max_iterations and not self._cancel_flag:
            iteration += 1
            self._state = AgentState.THINKING

            messages = [m.to_api_format() for m in self._messages]

            payload = {
                "model": self._ai_client._model,
                "max_tokens": self._ai_client._max_tokens,
                "system": self.SYSTEM_PROMPT,
                "messages": messages,
                "tools": self.tools_schema
            }

            thinking_step = AgentStep(step_type="thinking", content="Analyzing request...")
            self._steps.append(thinking_step)
            if on_step:
                on_step(thinking_step)

            response_data = self._make_tool_request(payload)

            if self._cancel_flag:
                break

            tool_uses = self._parse_tool_use(response_data)

            if not tool_uses:
                content = response_data.get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        final_content += block.get("text", "")

                if tools_used:
                    final_content = clean_tool_confirmation(
                        final_content,
                        last_tool_name=last_tool_name,
                        last_tool_input=last_tool_input,
                        last_result=last_tool_result,
                        user_message=user_message
                    )

                if final_content:
                    assistant_msg = Message(role="assistant", content=final_content, timestamp=time.time())
                    self._messages.append(assistant_msg)

                    if self._database and self._conversation_id:
                        self._database.add_message(
                            self._conversation_id,
                            "assistant",
                            final_content
                        )

                break

            DIRECT_ACTION_TOOLS = {
                "system_control",
                "app_open",
                "browser_open",
                "browser_search",
                "youtube_play",
                "create_and_send_pdf",
                "send_to_telegram",
                "desktop_icon_position"
            }

            for tool_idx, tool_use in enumerate(tool_uses):
                tool_name = tool_use["name"]
                tool_input = tool_use["input"]
                last_tool_name = tool_name
                last_tool_input = tool_input

                tool_step = AgentStep(
                    step_type="tool_use",
                    content=f"Using tool: {tool_name}",
                    tool_name=tool_name,
                    tool_input=tool_input
                )
                self._steps.append(tool_step)
                if on_step:
                    on_step(tool_step)

                result = self._execute_tool_with_permission(
                    tool_name,
                    tool_input,
                    permission_callback=on_permission
                )

                if result.requires_permission and self._pending_permission:
                    approved = self._pending_permission.wait(timeout=300)

                    if approved:
                        self._state = AgentState.EXECUTING

                        if self._pending_permission.session_allow:
                            self._gate.set_session_permission(tool_name, PermissionLevel.ALLOW)

                        if self._database and self._conversation_id:
                            exec_id = self._database.log_tool_execution(
                                self._conversation_id,
                                tool_name,
                                tool_input,
                                "approved"
                            )
                        else:
                            exec_id = None

                        exec_input = dict(tool_input)
                        exec_input["_approved"] = True
                        result = self._execute_tool(tool_name, exec_input)

                        if self._database and self._conversation_id and exec_id:
                            self._database.update_tool_execution(
                                exec_id,
                                {"output": result.output, "error": result.error},
                                "success" if result.success else "failed"
                            )
                    else:
                        result = ToolResult(
                            success=False,
                            output=None,
                            error="Permission denied by user"
                        )

                        if self._database and self._conversation_id:
                            self._database.log_tool_execution(
                                self._conversation_id,
                                tool_name,
                                tool_input,
                                "rejected"
                            )

                    self._pending_permission = None

                last_tool_result = result
                tools_used.append(tool_name)

                result_step = AgentStep(
                    step_type="tool_result",
                    content=f"Tool result: {result.output if result.success else result.error}",
                    tool_name=tool_name,
                    tool_result=result
                )
                self._steps.append(result_step)
                if on_step:
                    on_step(result_step)

                # Feed tool output back to the LLM as user observation for natural response generation
                result_repr = json.dumps(result.output, ensure_ascii=False) if result.success else f"Error: {result.error}"

                self._messages.append(Message(
                    role="assistant",
                    content=f"Using tool: {tool_name} with input: {json.dumps(tool_input, ensure_ascii=False)}"
                ))
                self._messages.append(Message(
                    role="user",
                    content=(
                        f"Tool '{tool_name}' execution result:\n{result_repr}\n\n"
                        f"Please provide a natural, concise, and helpful confirmation message to the user in their language (Arabic or English) confirming the action completed. "
                        f"Never output raw JSON or '[Tool Result]' blocks."
                    )
                ))

                # Direct action tool fast-return: bypass second LLM summarization call
                remaining_in_turn = len(tool_uses) - 1 - tool_idx
                if tool_name in DIRECT_ACTION_TOOLS and result.success and remaining_in_turn == 0:
                    confirm_msg = ""
                    if isinstance(result.output, dict):
                        confirm_msg = result.output.get("message") or result.output.get("summary") or ""
                    elif isinstance(result.output, str):
                        confirm_msg = result.output

                    final_content = clean_tool_confirmation(
                        confirm_msg,
                        last_tool_name=tool_name,
                        last_tool_input=tool_input,
                        last_result=result,
                        user_message=user_message
                    )
                    assistant_msg = Message(role="assistant", content=final_content, timestamp=time.time())
                    self._messages.append(assistant_msg)

                    if self._database and self._conversation_id:
                        self._database.add_message(
                            self._conversation_id,
                            "assistant",
                            final_content
                        )

                    self._state = AgentState.IDLE
                    return AgentResponse(
                        content=final_content,
                        steps=self._steps,
                        tools_used=tools_used,
                        success=True
                    )

        if tools_used:
            final_content = clean_tool_confirmation(
                final_content,
                last_tool_name=last_tool_name,
                last_tool_input=last_tool_input,
                last_result=last_tool_result,
                user_message=user_message
            )

        self._state = AgentState.IDLE

        default_fallback = "✅ تم إكمال العملية المطلوبة بنجاح!" if any('؀' <= char <= 'ۿ' for char in user_message) else "✅ I've completed the requested actions."
        return AgentResponse(
            content=final_content or default_fallback,
            steps=self._steps,
            tools_used=tools_used,
            success=True
        )

    def _make_tool_request(self, payload: Dict[str, Any], max_attempts: int = 3, backoff: float = 1.5) -> Dict[str, Any]:
        """Make a tool-enabled API request with automatic retry on gateway errors and timeouts."""
        import urllib.request
        import urllib.error
        import socket
        import json as json_module

        url = f"{self._ai_client._base_url}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._ai_client._api_key,
            "anthropic-version": "2023-06-01"
        }

        data = json_module.dumps(payload).encode("utf-8")

        for attempt in range(1, max_attempts + 1):
            if self._cancel_flag:
                raise RuntimeError("Execution cancelled")

            try:
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self._ai_client._timeout) as response:
                    return json_module.loads(response.read().decode("utf-8"))

            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504) and attempt < max_attempts and not self._cancel_flag:
                    time.sleep(backoff)
                    continue
                raise

            except (urllib.error.URLError, TimeoutError, socket.timeout):
                if attempt < max_attempts and not self._cancel_flag:
                    time.sleep(backoff)
                    continue
                raise

    def respond_to_permission(self, approved: bool, remember: bool = False, session_allow: bool = False) -> None:
        """Respond to a pending permission request."""
        if self._pending_permission:
            tool_name = self._pending_permission.tool_name
            if approved:
                self._pending_permission.approve(session_allow=session_allow)
                if session_allow:
                    self._gate.set_session_permission(tool_name, PermissionLevel.ALLOW)
                elif remember and self._database:
                    self._database.set_tool_permission(tool_name, "allow")
            else:
                self._pending_permission.reject()
                if remember and self._database:
                    self._database.set_tool_permission(tool_name, "deny")

    def cancel(self) -> None:
        """Cancel current execution."""
        self._cancel_flag = True
        if self._pending_permission:
            self._pending_permission.reject()
        self._state = AgentState.IDLE

    def clear_history(self) -> None:
        """Clear conversation history."""
        self._messages = []
        self._steps = []
