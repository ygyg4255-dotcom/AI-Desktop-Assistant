"""Agent Executor - Core Agent Loop for AI Desktop Assistant.

Implements a Claude Code-inspired agent loop:
User Prompt -> Agent analyzes -> Selects Tool -> Executes -> Verifies Output -> Final Response
"""
import json
import time
import re
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable, Union
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import Future

from src.agent.tools import ToolRegistry, ToolResult, create_default_registry, strip_internal_keys
from src.agent.permissions import PermissionGate, PermissionLevel
from src.agent.approvals import (
    ApprovalManager,
    CHANNEL_HUD,
    CHANNEL_LOCAL,
    CHANNEL_TELEGRAM,
    CHANNEL_VOICE,
    DEFAULT_TTL,
)
from src.agent.history import HISTORY_FILENAME, PersistentHistory
from src.agent.vision import CAMERA_PLACEHOLDER, SCREENSHOT_PLACEHOLDER, strip_image_data
from src.agent.memory import ConversationMemory
from src.agent import memory_store
from src.services import scheduler
from src.agent.verifiers import verify_result
from src.agent.tool_prompts import strip_action_tag, tag_reply, tool_result_prompt
from src.services.ai_client import AIClient, Message
from src.services.hud_status import update_hud_status
from src.services.sentence_chunker import SentenceChunker
from src.config.log import get_logger, redact_secrets
from src.config.paths import get_desktop_dir
from src.config.settings import get_config_value

APPROVED_OUTCOME_MAX_CHARS = 300
# Tools whose success reply is built locally (no second LLM call).
DIRECT_ACTION_TOOLS = frozenset({
    "system_control", "app_open", "browser_open", "browser_search", "youtube_play", "create_and_send_pdf",
    "send_to_telegram", "desktop_icon_position", "move_application_window", "capture_and_inspect_screen",
    "capture_camera", "camera_start", "camera_stop", "whatsapp_send", "whatsapp_read", "clipboard_manager",
})
# Read-only SAFE tools that are safe to run twice; retried once on a transient error.
RETRYABLE_TOOLS = frozenset({"file_search", "file_list", "system_info", "process_list", "note_list",
                             "note_read", "todo_list", "list_reminders"})
_TRANSIENT_RE = re.compile(r"timed? ?out|temporar|busy|being used by another process|try again|connection (?:reset|aborted)", re.I)
# A request listing several actions ("a, b and then c"); such turns must not fast-return after one tool.
_COMPOUND_RE = re.compile(r"\b(?:and|then|also)\b|[,;،]|\bثم\b|\bو\S", re.IGNORECASE)


def _is_compound(message: str) -> bool:
    return bool(_COMPOUND_RE.search(message or ""))

logger = get_logger(__name__)
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
    # True when ``content`` was already dispatched sentence-by-sentence to the
    # ``on_sentence`` callback while streaming; callers must not speak it again.
    streamed_speech: bool = False


class SpeechStreamer:
    """Turns streamed text deltas into whole sentences for the TTS queue.

    Text that looks like a leaked plain-text tool call ("Using tool: ...") or
    raw JSON is never dispatched; the executor still post-processes the final
    text exactly as before.
    """

    def __init__(self, on_sentence: Callable[[str], None]):
        self._on_sentence = on_sentence
        self._chunker = SentenceChunker()
        self._suppressed = False
        self.spoke = False

    def feed(self, delta: str) -> None:
        self._dispatch(self._chunker.feed(delta))

    def flush(self) -> None:
        self._dispatch(self._chunker.flush())

    def _dispatch(self, sentences: List[str]) -> None:
        for sentence in sentences:
            if self._suppressed:
                return
            if "Using tool:" in sentence or sentence.lstrip().startswith(("{", "[")):
                self._suppressed = True
                return
            try:
                self._on_sentence(sentence)
                self.spoke = True
            except Exception as err:  # a TTS hiccup must never break the agent turn
                logger.debug("on_sentence callback failed: %s", err)


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


# Tools whose output is outside content (web/files/terminal/screen): `remember` is refused after them in the same turn.
_EXTERNAL_TOOL_RE = re.compile(r"web|browse|fetch|search|read|http|terminal|screenshot|screen|ocr|clipboard|youtube|claude_code|camera|vision|telegram", re.I)
from src.agent.local_router import match_local_intent  # noqa: E402,F401 (tests patch executor.match_local_intent)


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
        or "Using tool:" in trimmed
        or (trimmed.startswith("{") and trimmed.endswith("}"))
        or (trimmed.startswith("[") and trimmed.endswith("]"))
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

    elif last_tool_name == "capture_and_inspect_screen":
        active = out_dict.get("active_window", "")
        sent = out_dict.get("sent_to_telegram", False)
        if is_arabic:
            telegram_txt = "، وتم إرسال لقطة الشاشة إلى تيليجرام" if sent else ""
            return f"✅ أرى شاشتك بوضوح! النافذة النشطة حالياً هي '{active or 'سطح المكتب'}'{telegram_txt}."
        else:
            telegram_txt = ", and sent the live screenshot to your Telegram" if sent else ""
            return f"✅ I can see your screen! Currently active window is '{active or 'Desktop'}'{telegram_txt}."

    elif last_tool_name == "move_application_window":
        title = out_dict.get("window_title", "")
        pos = out_dict.get("position", "")
        pos_ar_map = {
            "right": "يمين الشاشة",
            "left": "يسار الشاشة",
            "center": "وسط الشاشة",
            "top": "أعلى الشاشة",
            "bottom": "أسفل الشاشة",
            "top_right": "أعلى يمين الشاشة",
            "top_left": "أعلى يسار الشاشة",
            "bottom_right": "أسفل يمين الشاشة",
            "bottom_left": "أسفل يسار الشاشة",
            "maximize": "ملء الشاشة",
            "minimize": "شريط المهام",
        }
        pos_ar = pos_ar_map.get(pos, "الموضع المطلوب")
        pos_en = pos.replace("_", " ").title()
        if is_arabic:
            return f"✅ تم تحريك وضبط نافذة '{title or 'التطبيق'}' في {pos_ar} بنجاح!"
        else:
            return f"✅ Successfully moved window '{title or 'Application'}' to {pos_en}!"

    elif last_tool_name == "youtube_play_and_summarize":
        title = out_dict.get("title", "")
        if is_arabic:
            return f"✅ تم استخراج نص فيديو '{title or 'YouTube'}' وتلخيصه."
        else:
            return f"✅ Processed and summarized YouTube video '{title or 'YouTube'}'."

    elif last_tool_name == "whatsapp_send":
        rec = out_dict.get("recipient", "")
        if is_arabic:
            return f"✅ تم إرسال رسالة واتساب إلى '{rec}' بنجاح!"
        else:
            return f"✅ Successfully sent WhatsApp message to '{rec}'!"

    elif last_tool_name == "whatsapp_read":
        chat = out_dict.get("chat", "") or last_tool_input.get("contact_or_chat", "")
        reply = out_dict.get("latest_reply") or out_dict.get("reply", "")
        if not reply:
            msgs = out_dict.get("messages", [])
            if isinstance(msgs, list) and msgs:
                reply = msgs[-1]
            elif isinstance(msgs, str):
                reply = msgs
            elif isinstance(output, str):
                reply = output
        telegram_sent = out_dict.get("sent_to_telegram", False)
        t_note_ar = " (تم إرسال الرد إلى تيليجرام 📱)" if telegram_sent else ""
        t_note_en = " (Reply delivered to Telegram 📱)" if telegram_sent else ""
        if is_arabic:
            if reply:
                return f"✅ أحدث رد من {chat or 'المحادثة'}{t_note_ar}:\n\n{reply}"
            return f"✅ تم فحص محادثة {chat or 'المحادثة'} بنجاح{t_note_ar}."
        else:
            if reply:
                return f"✅ Latest reply from {chat or 'Active Chat'}{t_note_en}:\n\n{reply}"
            return f"✅ Checked conversation for {chat or 'Active Chat'}{t_note_en}."

    elif last_tool_name == "clipboard_manager":
        msg = out_dict.get("message")
        if msg:
            return msg
        action = out_dict.get("action", "")
        if is_arabic:
            return f"✅ تم تنفيذ عملية الحافظة ({action}) بنجاح."
        else:
            return f"✅ Successfully performed clipboard action ({action})."

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


def intercept_raw_code_or_payload(
    text: str,
    user_message: str,
    executor: "AgentExecutor"
) -> Optional[tuple]:
    """Intercept raw JSON tool calls or raw Python code leaked as text, execute silently, and return human confirmation."""
    if not text:
        return None

    # Clean up inspect_wa/ocr temporary files if present in root directory
    for tmp_f in (Path("inspect_wa.ps1"), Path("inspect_wa_out.json"), Path("inspect_ocr.ps1"), Path("inspect_ocr_out.json")):
        try:
            if tmp_f.exists():
                tmp_f.unlink()
        except Exception:
            logger.debug("ignored error", exc_info=True)

    trimmed = text.strip()
    is_arabic = any('؀' <= char <= 'ۿ' for char in user_message)

    # 0. Intercept "Using tool: <tool_name> with input: {...}" plain text tool call
    using_tool_match = re.search(r'Using tool:\s*([a-zA-Z0-9_]+)\s*with input:\s*(\{.*\})', trimmed, re.DOTALL)
    if not using_tool_match:
        using_tool_match = re.search(r'Using tool:\s*([a-zA-Z0-9_]+)\s*with input:\s*(\{.*?\})', trimmed, re.DOTALL)
    if using_tool_match:
        tool_name = using_tool_match.group(1).strip()
        args_raw = using_tool_match.group(2).strip()
        try:
            try:
                parsed_args = json.loads(args_raw)
            except Exception:
                m_sub = re.search(r'(\{[\s\S]*?\})', args_raw)
                parsed_args = json.loads(m_sub.group(1)) if m_sub else {}

            if isinstance(parsed_args, dict) and executor.registry.get(tool_name):
                res = executor.registry.execute(tool_name, **parsed_args)
                if res.requires_permission:
                    return executor._request_approval(tool_name, parsed_args, res, user_message), res
                confirmation = clean_tool_confirmation(
                    "",
                    last_tool_name=tool_name,
                    last_tool_input=parsed_args,
                    last_result=res,
                    user_message=user_message
                )
                return confirmation, res
        except Exception:
            logger.debug("ignored error", exc_info=True)

    # 1. Check for JSON tool payload (raw or wrapped in ```json ... ```)
    json_candidate = None
    if "{" in trimmed and "}" in trimmed:
        json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', trimmed)
        if json_match:
            json_candidate = json_match.group(1).strip()
        elif trimmed.startswith("{") and trimmed.endswith("}"):
            json_candidate = trimmed
        else:
            m = re.search(r'(\{(?:[^{}]|(?:\{[^{}]*\}))*\})', trimmed)
            if m:
                json_candidate = m.group(1).strip()

    if json_candidate:
        try:
            parsed = json.loads(json_candidate)
            if isinstance(parsed, dict):
                tool_name = parsed.get("name") or parsed.get("tool") or parsed.get("tool_name")
                tool_input = parsed.get("parameters") or parsed.get("input") or parsed.get("arguments") or parsed.get("tool_input") or {}
                if not tool_name and "command" in parsed:
                    tool_name = "terminal_execute"
                    tool_input = {"command": parsed["command"]}

                # If desktop_icon_position was called for a window, redirect to move_application_window
                if tool_name == "desktop_icon_position":
                    target = str(tool_input.get("target", ""))
                    if any(w in target.lower() for w in ("نافذة", "نافذه", "متصفح", "المتصفح", "window", "browser", "omniroute")):
                        tool_name = "move_application_window"
                        tool_input = {"window_query": target, "position": tool_input.get("position", "right")}

                if tool_name and executor._registry.get(tool_name):
                    res = executor._execute_tool(tool_name, tool_input)
                    if res.requires_permission:
                        return executor._request_approval(tool_name, tool_input, res, user_message), res
                    confirmation = clean_tool_confirmation(
                        "",
                        last_tool_name=tool_name,
                        last_tool_input=tool_input,
                        last_result=res,
                        user_message=user_message
                    )
                    return confirmation, res
        except Exception:
            logger.debug("ignored error", exc_info=True)

    # 2. Check for raw Python code leaks (ctypes.windll, win32gui, win32con, MoveWindow, SetWindowPos)
    has_raw_code = (
        "win32gui." in trimmed
        or "win32con." in trimmed
        or "ctypes.windll" in trimmed
        or "MoveWindow(" in trimmed
        or "SetWindowPos(" in trimmed
        or "GetForegroundWindow(" in trimmed
        or "import win32" in trimmed
        or "import ctypes" in trimmed
    )

    if has_raw_code:
        pos = "right"
        if any(w in user_message.lower() for w in ("يمين", "right")):
            pos = "right"
        elif any(w in user_message.lower() for w in ("يسار", "شمال", "left")):
            pos = "left"
        elif any(w in user_message.lower() for w in ("وسط", "منتصف", "center", "middle")):
            pos = "center"
        elif any(w in user_message.lower() for w in ("أعلى", "اعلى", "top")):
            pos = "top"
        elif any(w in user_message.lower() for w in ("أسفل", "اسفل", "bottom")):
            pos = "bottom"
        elif any(w in user_message.lower() for w in ("تكبير", "maximize")):
            pos = "maximize"
        elif any(w in user_message.lower() for w in ("تصغير", "minimize")):
            pos = "minimize"

        res = executor._execute_tool("move_application_window", {
            "window_query": user_message,
            "position": pos
        })

        if res.success:
            title = res.output.get("window_title", "")
            if is_arabic:
                msg = f"✅ تم تحريك وضبط نافذة '{title}' إلى {pos} بنجاح!"
            else:
                msg = f"✅ Successfully moved window '{title}' to {pos}!"
            return msg, res
        else:
            if is_arabic:
                return "✅ تم تنفيذ الأمر بنجاح.", res
            else:
                return "✅ Action executed successfully.", res

    return None


def sanitize_final_reply(content: str, user_message: str) -> str:
    """Ensure no raw code, win32 calls, or JSON objects leak to the user interface."""
    # Clean up inspect_wa/ocr temporary files if present in root directory
    for tmp_f in (Path("inspect_wa.ps1"), Path("inspect_wa_out.json"), Path("inspect_ocr.ps1"), Path("inspect_ocr_out.json")):
        try:
            if tmp_f.exists():
                tmp_f.unlink()
        except Exception:
            logger.debug("ignored error", exc_info=True)

    trimmed = strip_action_tag((content or "").strip()).strip()
    if not trimmed:
        is_ar = any('؀' <= c <= 'ۿ' for c in user_message)
        return "✅ تم تنفيذ العملية بنجاح." if is_ar else "✅ Completed successfully."

    # Intercept raw "Using tool: <name> with input: {...}" format
    if "Using tool:" in trimmed:
        trimmed = re.sub(r'Using tool:\s*[a-zA-Z0-9_]+(?:\s*with input:\s*\{.*?\})?', '', trimmed, flags=re.DOTALL).strip()
        trimmed = re.sub(r'Using tool:\s*[a-zA-Z0-9_]+(?:\s*with input:\s*\{.*\})?', '', trimmed, flags=re.DOTALL).strip()
        trimmed = re.sub(r'Using tool:\s*[a-zA-Z0-9_]+', '', trimmed).strip()
        if not trimmed:
            is_ar = any('؀' <= c <= 'ۿ' for c in user_message)
            return "✅ تم تنفيذ العملية المطلوبة بنجاح!" if is_ar else "✅ Completed requested action successfully."

    # Intercept raw JSON/Python list leak
    if (trimmed.startswith("[") and trimmed.endswith("]")) or trimmed.startswith("```json\n["):
        try:
            import json
            clean_str = trimmed
            if clean_str.startswith("```json"):
                clean_str = re.sub(r'^```json\s*', '', clean_str)
                clean_str = re.sub(r'\s*```$', '', clean_str)
            parsed = json.loads(clean_str.strip())
            if isinstance(parsed, list) and parsed:
                latest = str(parsed[-1]).strip()
                is_ar = any('؀' <= c <= 'ۿ' for c in user_message)
                return f"✅ أحدث رد:\n\n{latest}" if is_ar else f"✅ Latest reply:\n\n{latest}"
        except Exception:
            logger.debug("ignored error", exc_info=True)

    is_raw_code_leak = (
        "win32gui." in trimmed
        or "win32con." in trimmed
        or "ctypes.windll" in trimmed
        or "import win32" in trimmed
        or "import ctypes" in trimmed
        or "MoveWindow(" in trimmed
        or "SetWindowPos(" in trimmed
        or "GetForegroundWindow(" in trimmed
        or (trimmed.startswith("{") and trimmed.endswith("}") and ('"name":' in trimmed or '"command":' in trimmed))
        or (trimmed.startswith("```json") and ('"name":' in trimmed or '"command":' in trimmed))
    )

    if is_raw_code_leak:
        is_ar = any('؀' <= c <= 'ۿ' for c in user_message)
        if any(w in user_message.lower() for w in ("نافذة", "نافذه", "متصفح", "المتصفح", "window", "browser", "omniroute")):
            return "✅ تم ضبط وتحريك نافذة التطبيق/المتصفح بنجاح!" if is_ar else "✅ Successfully adjusted the application window position!"
        return "✅ تم إكمال العملية المطلوبة بنجاح." if is_ar else "✅ Completed requested action successfully."

    return trimmed


class AgentExecutor:
    """Core agent execution engine."""

    SYSTEM_PROMPT = """You are Altron (ألترون in Arabic), an intelligent AI Desktop Assistant with access to system tools, desktop controls, and screen vision.

You help users interact with their Windows computer through natural language. When asked who you are or to introduce yourself, ALWAYS identify yourself as Altron (ألترون), the intelligent desktop assistant.

WINDOW MANAGEMENT vs DESKTOP ICONS (CRITICAL DISTINCTION):
- When the user asks to move, snap, maximize, or minimize an open APPLICATION WINDOW (نافذة), BROWSER (متصفح), or running program (e.g. 'ضع نافذة المتصفح المفتوح على موقع omniroute في يمين الشاشة'), ALWAYS use the `move_application_window` tool. NEVER use `desktop_icon_position` for windows!
- Use `desktop_icon_position` ONLY for shortcut icons, files, or folders located on the Windows Desktop wallpaper background. NEVER confuse open application windows with desktop background icons!

SCREEN VISION & CAPABILITIES:
- You CAN see the user's PC screen: `capture_and_inspect_screen` captures the desktop and attaches the screenshot as an image you can actually look at (text, errors, buttons, UI elements).
- When asked if you can see the screen, what is on the screen, to read an error, or to inspect the user's workspace, NEVER say "No" or claim that you lack vision or eyes. ALWAYS use `capture_and_inspect_screen`, then describe what you really see in the image. Never invent screen content you have not captured.
- `capture_and_inspect_screen` also delivers the screenshot to the user's Telegram chat when requested (set send_to_telegram=false if the user only wants you to look).

WEBCAM VISION:
- You CAN see through the user's webcam: `capture_camera` takes one photo and attaches it as an image (instant when the background camera is on). When the user asks you to look through the camera, see them, or identify/describe something they are holding or showing, ALWAYS call it, then answer ONLY the question asked (e.g. "what am I holding?" -> just name the item; no background, room or location unless asked). For "what did I move?" / "what changed?" call it with `compare_seconds_ago` (e.g. 5) to get an earlier frame and the current one, and compare them. `camera_start` / `camera_stop` turn the background camera on/off when asked. Never claim you have no camera access, and only look when the user asks: camera frames are never sent to you otherwise.

GUI AUTOMATION:
- You CAN operate the mouse and keyboard. `gui_click` and `gui_scroll` run immediately when the user asks. `gui_type` and `gui_hotkey` are shown to the user for approval first: call them directly (never ask permission in text; the gate sends the approval card). To open/toggle the VS Code terminal send `ctrl+j`, never ctrl+backtick (fails on non-US layouts). If declined or pending, say so and do NOT claim it happened.
- Look first: capture the screen, then pass `gui_click` the x/y pixel position on the screenshot you were shown. Do NOT scale coordinates yourself: the tool maps them to the real screen using the capture's `screenshot_size` and `screen_size`. Act, then capture again to verify the result.
- When the user explicitly asks to click a visible UI element (e.g. "click System"), capture the screen if you do not have a current view, then call `gui_click` in the same turn. Do NOT ask "Should I click it?" for click or scroll requests.
- NEVER type into password, PIN, passcode or other credential fields, never type secrets, API keys or tokens, and never handle credentials: if a sign-in or password prompt appears, stop and ask the user to enter it themselves.
- Prefer dedicated tools (app_open, whatsapp_send, ...) over GUI automation when one exists.

YOUTUBE PLAY & SUMMARIZATION:
- When the user asks to summarize, explain, or get key insights/takeaways from a YouTube video (e.g. 'اعطني ملخص هذا الفيديو', 'لخص هذا الفيديو', 'summarize this video'):
  - ALWAYS use the `youtube_play_and_summarize` tool.
  - Set `play=False` whenever the user asks ONLY to summarize or explain the video WITHOUT explicitly asking to play/open/watch it. In this case, do NOT launch the browser; extract the transcript silently in the background and generate the summary.
  - Set `play=True` ONLY when the user explicitly asks to play, open, or watch the video (e.g. 'شغل ولخص', 'افتح ولخص', 'play and summarize').
  - Synthesize a comprehensive, well-structured summary in the user's language (Arabic or English) with bullet points, main ideas, and key takeaways.

WINDOWS CLIPBOARD MANAGEMENT:
- Use `clipboard_manager` to read, copy, paste, view history, or clear the Windows clipboard.
- Actions: 'get', 'set', 'paste', 'copy_selected', 'history', 'clear'.

WHATSAPP DESKTOP AUTOMATION:
- When the user asks to send a WhatsApp message (e.g. 'ارسل رسالة واتساب الى ...', 'send whatsapp message to ...'), ALWAYS use the `whatsapp_send` tool.
- When the user asks to read WhatsApp messages, what someone sent on WhatsApp, or inspect a chat (e.g. 'اقرأ رسائل واتساب من ...', 'ماذا أرسل لي فلان في واتساب', 'read whatsapp messages from ...'), ALWAYS use the `whatsapp_read` tool.
- Supports phone numbers (Moroccan 06/07 numbers automatically converted to 212) or contact/group names.

IMPORTANT RULES:
1. When asked to move, snap, position, maximize, or minimize an open APPLICATION WINDOW or BROWSER (نافذة أو متصفح أو برنامج), ALWAYS use the `move_application_window` tool.
2. When asked to visually move or position desktop shortcut icons or files on the Windows Desktop wallpaper background, use the `desktop_icon_position` tool.
3. When asked if you can see the screen, what is open/visible, what an error says, or to capture/take a screenshot, ALWAYS use the `capture_and_inspect_screen` tool. For clicking, typing, hotkeys or scrolling on screen use the `gui_*` tools (typing/hotkeys need approval; never for credentials). When asked to look through the camera/webcam, use `capture_camera`.
4. When asked to adjust volume (volume_up, volume_down, volume_mute, set_volume), screen brightness (brightness_up, brightness_down, set_brightness), or open Windows settings, ALWAYS use the `system_control` tool for instant execution. NEVER use terminal_execute or PowerShell for volume or brightness.
5. When asked to summarize or explain a YouTube video, use `youtube_play_and_summarize` with `play=False` (unless the user explicitly asks to play/open it). When asked ONLY to play music or a video without summarization, use `youtube_play`.
6. When asked to send a WhatsApp message, use `whatsapp_send`. When asked to read or check WhatsApp messages, use `whatsapp_read`.
7. When asked to read, copy, paste, view history, or clear clipboard content, ALWAYS use the `clipboard_manager` tool.
8. When asked to create, write, or generate a PDF report or send documents to the user's phone, use `create_and_send_pdf` or `send_to_telegram`.
7. Analyze the user's request carefully before using any tool.
8. Only use tools when necessary - simple questions don't require tools.
9. Do not narrate before using a tool; just act. Keep replies to 1-2 short sentences unless the user asks for detail, and never open with filler like "Very well, sir."
10. For file operations, always verify paths exist before reading.
11. For terminal commands, be cautious and explain the command.
12. If a tool fails, explain the error and suggest alternatives.
13. After executing any tool, ALWAYS provide a friendly, natural, and concise confirmation message in the user's language (Arabic or English), e.g., '✅ تم نسخ 11,400 ملف إلى مجلد her بنجاح!'. NEVER output raw JSON, '[Tool Result]', Python scripts, or technical data dumps to the user.
13b. To list/show files or folders (Desktop, Documents, any folder) ALWAYS use `file_list` (directory="desktop" for the Desktop). NEVER use terminal_execute/PowerShell for listing.
14. CRITICAL VOICE RULE: Your text responses are automatically spoken aloud by your built-in internal voice synthesizer (`TTSManager`). NEVER invoke `terminal_execute` or PowerShell `System.Speech` to speak or demonstrate your voice—simply reply with plain text and your internal voice engine will speak it immediately.

15. TRUTHFULNESS (CRITICAL): NEVER claim, imply or describe an action as done (opened, closed, checked, sent, moved, found...) unless you called a tool in THIS turn and its result confirmed it. If no tool ran, say what you could do or ask; do not pretend. If a tool failed or returned partial data, say so plainly (e.g. "checked 50 of 254 processes"). Describe only what the tool result actually contains.
16. Earlier assistant messages that carry an "[Actions run this turn: ...]" tag were added by the system and list the tools that really ran. Assistant messages WITHOUT such a tag that claim an action did not run any tool, so never rely on them. Never write that tag yourself.
17. PREVIOUS-SESSION MEMORY is secondary background only (a "[Previous session context — for reference only]" block, if present). Use it for the user's preferences and continuity, prioritise the current conversation, and NEVER assume windows, apps, files or earlier tool results from a past session are still valid: check live with a tool first.

Available tools are provided in the tool definitions. Use them responsibly."""

    def __init__(
        self,
        ai_client: AIClient,
        database: Optional[Database] = None,
        registry: Optional[ToolRegistry] = None,
        gate: Optional[PermissionGate] = None,
        memory: Optional[ConversationMemory] = None,
        approvals: Optional[ApprovalManager] = None,
        history: Optional[PersistentHistory] = None,
    ):
        self._ai_client = ai_client
        # When set, gated tool calls become non-blocking approval requests instead
        # of an in-process blocking prompt (required for HUD / voice / Telegram).
        self._approvals: Optional[ApprovalManager] = None
        self._origin: str = CHANNEL_LOCAL
        if approvals is not None:
            self.attach_approvals(approvals)
        self._database = database
        self._registry = registry or create_default_registry()
        self._gate = gate or PermissionGate()
        self._memory = memory or ConversationMemory()
        # Soft cross-session memory: snapshot the previous session as reference text,
        # then start this session with an empty working memory so old turns are never
        # replayed as live conversation.
        self._history = history or PersistentHistory(self._memory.path.parent / HISTORY_FILENAME)
        self._previous_context = self._history.load_reference()
        self._persona_cache: Optional[str] = None
        self._tool_failures: Dict[str, int] = {}
        self._turn_tainted = False
        self._memory.clear()
        self._state = AgentState.IDLE
        self._conversation_id: Optional[int] = None
        self._messages: List[Message] = []
        self._steps: List[AgentStep] = []
        self._pending_permission: Optional[PermissionRequest] = None
        self._cancel_flag = False
        # Worst-case Steam add-to-cart: search + open + 5 inspects + 4 scrolls + click + reply.
        try:
            self._max_iterations = max(10, int(get_config_value("MAX_AGENT_STEPS", default=20)))
        except (TypeError, ValueError):
            self._max_iterations = 20
        # Set by _approval_response when an approved tool should resume the interrupted task.
        self._resume_ctx: Optional[Dict[str, Any]] = None
        # Called as (AgentResponse, origin) with the final reply of a resumed task.
        self.on_resume_complete: Optional[Callable[[Any, str], None]] = None

        # Clean up any temporary inspect_wa/ocr files in root directory
        for tmp_f in (Path("inspect_wa.ps1"), Path("inspect_wa_out.json"), Path("inspect_ocr.ps1"), Path("inspect_ocr_out.json")):
            try:
                if tmp_f.exists():
                    tmp_f.unlink()
            except Exception:
                logger.debug("ignored error", exc_info=True)

    def _get_system_prompt(self) -> str:
        """Dynamically load persona prompt from prompts/system_prompt.txt and prepend to SYSTEM_PROMPT."""
        persona = self._persona_cache
        prompt_paths = [] if persona is not None else [
            Path("prompts/system_prompt.txt"),
            Path(__file__).resolve().parent.parent.parent / "prompts" / "system_prompt.txt",
        ]
        for p in prompt_paths:
            if p.exists():
                try:
                    persona = p.read_text(encoding="utf-8").strip()
                    if persona:
                        break
                except Exception:
                    logger.debug("ignored error", exc_info=True)

        persona = self._persona_cache = persona or ""
        nl = chr(10) * 2
        prompt = f"{persona}{nl}{self.SYSTEM_PROMPT}" if persona else self.SYSTEM_PROMPT
        # Injected here (not in the persona file) to stay under the 6900-char persona cap.
        prompt += (f"{nl}Primary Desktop path: {get_desktop_dir()}. 'desktop' / 'سطح المكتب' always means this "
                   "folder (also in terminal_execute), never C:\\Users\\<user>\\Desktop.")
        if self._previous_context:
            prompt = f"{prompt}{nl}{self._previous_context}"
        return prompt

    def _append_tool_feedback(self, tool_name: str, result_repr: str, result: ToolResult) -> None:
        """Hand a tool result back to the model; a screenshot goes in as a multimodal image block.

        Only the newest screenshot stays in the in-memory message list (older ones become
        a text placeholder), and image data is never passed to ``_remember``/history.
        """
        text = tool_result_prompt(tool_name, result_repr, result.success, self._tool_failures.get(tool_name, 0))
        if result.success and result.image:
            kinds = self.__dict__.setdefault("_image_kinds", {})  # id(message) -> "camera" | "screenshot"
            for msg in self._messages:
                placeholder = CAMERA_PLACEHOLDER if kinds.get(id(msg)) == "camera" else SCREENSHOT_PLACEHOLDER
                msg.content = strip_image_data(msg.content, placeholder)
            new_msg = Message(
                role="user",
                content=[result.image, *getattr(result, "extra_content", []), {"type": "text", "text": text}],
            )
            kinds[id(new_msg)] = result.image_kind
            self._messages.append(new_msg)
        else:
            self._messages.append(Message(role="user", content=text))

    def _remember(self, role: str, content: str, outcomes: Optional[List[str]] = None) -> None:
        """Record a turn in working memory and persistent history; assistant turns carry the action tag."""
        stored = tag_reply(content, outcomes or []) if role == "assistant" else content
        self._memory.add_message(role, stored)
        self._history.append(role, stored)

    @staticmethod
    def _log_outcome(user_message: str, tool_outcomes: List[str]) -> None:
        """Zero-LLM outcome memory for a tool-using turn: truncated request + tools + ok/failed."""
        used = [o for o in tool_outcomes if not o.startswith("remember ")]
        if not used:
            return
        status = "failed" if any(o.endswith("failed") for o in used) else "ok"
        tools = ", ".join(dict.fromkeys(o.rsplit(" ", 1)[0] for o in used))
        request = " ".join(user_message.split())[:100]
        try:
            memory_store.get_store().add("outcome", f'"{request}" -> {tools}: {status}')
        except Exception:
            logger.debug("outcome logging failed", exc_info=True)

    @property
    def memory(self) -> ConversationMemory:
        """Get the conversation memory instance."""
        return self._memory

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

    @property
    def registry(self) -> ToolRegistry:
        """Get the tool registry."""
        return self._registry

    def _intercept_raw_code_or_payload(self, text: str, user_message: str) -> Optional[tuple]:
        """Intercept raw JSON tool calls, raw code leaks, or 'Using tool: ...' text."""
        return intercept_raw_code_or_payload(text, user_message, self)

    def _sanitize_final_reply(self, content: str, user_message: str) -> str:
        """Sanitize final assistant reply to prevent any technical leaks."""
        return sanitize_final_reply(content, user_message)

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

    def _execute_tool(self, tool_name: str, tool_input: Dict[str, Any], approved: bool = False) -> ToolResult:
        """Execute a tool by name."""
        tool = self._registry.get(tool_name)
        if not tool:
            return ToolResult(
                success=False,
                output=None,
                error=f"Unknown tool: {tool_name}"
            )

        return self._run_tool(tool_name, tool, tool_input, approved)

    def _run_tool(self, tool_name: str, tool: Any, tool_input: Dict[str, Any], approved: bool = False) -> ToolResult:
        """tool.run plus one retry (read-only tools, transient errors), post-run verification and the failure counter."""
        if tool_name == "remember" and not approved and self._turn_tainted:
            return ToolResult(success=False, output=None, error=(
                "Cannot save memories after reading external content (prompt-injection guard); "
                "ask the user to say it directly."))
        if _EXTERNAL_TOOL_RE.search(tool_name):
            self._turn_tainted = True
        result = tool.run(tool_input, approved=approved)
        if (not result.success and not result.requires_permission and tool_name in RETRYABLE_TOOLS
                and _TRANSIENT_RE.search(result.error or "")):
            logger.info("Retrying %s once after transient error: %s", tool_name, result.error)
            time.sleep(0.3)
            result = tool.run(tool_input, approved=approved)
        if result.success:
            detail = verify_result(tool_name, tool_input, result)
            if detail:
                result.success, result.error = False, detail
        if not result.success and not result.requires_permission:
            self._tool_failures[tool_name] = self._tool_failures.get(tool_name, 0) + 1
        return result

    # -- out-of-band approvals (audit S5a) -------------------------------------
    @property
    def approvals(self) -> Optional[ApprovalManager]:
        return self._approvals

    def attach_approvals(self, manager: ApprovalManager) -> None:
        """Route gated tool calls through ``manager``; approved calls run via run_approved."""
        self._approvals = manager
        manager.set_runner(self.run_approved)

    def run_approved(self, tool_name: str, tool_input: Dict[str, Any]) -> ToolResult:
        """Run a tool the user approved out-of-band, with exactly the stored args."""
        exec_id = None
        if self._database and self._conversation_id:
            try:
                exec_id = self._database.log_tool_execution(self._conversation_id, tool_name, tool_input, "approved")
            except Exception:
                exec_id = None
        try:
            result = self._execute_tool(tool_name, tool_input, approved=True)
        except Exception as err:
            result = ToolResult(success=False, output=None, error=str(err) or type(err).__name__)
            logger.exception("Approved tool %s raised", tool_name)
        if exec_id:
            try:
                self._database.update_tool_execution(
                    exec_id,
                    {"output": result.output, "error": result.error},
                    "success" if result.success else "failed",
                )
            except Exception:
                logger.debug("Could not update tool execution record", exc_info=True)
        self._record_approved_outcome(tool_name, result)
        ctx, self._resume_ctx = self._resume_ctx, None
        if ctx:
            self._resume_after_approval(ctx, tool_name, result)
        return result

    def _resume_after_approval(self, ctx: Dict[str, Any], tool_name: str, result: ToolResult) -> None:
        """Re-enter the agent loop so the steps after an approved tool still run."""
        done = ", ".join(ctx["done"] + [f"{tool_name} {'ok' if result.success else 'failed'}"])
        prompt = (f"[System] Approved step finished. Steps done so far: {done}. "
                  "Continue with the remaining steps of my original request, then give one short summary.")
        self._cancel_flag = False
        try:
            resp = self._process_sync(ctx["user_message"], origin=ctx["origin"], resume_prompt=prompt)
        except Exception:
            logger.exception("Resuming after approved %s failed", tool_name)
            return
        if self.on_resume_complete:
            try:
                self.on_resume_complete(resp, ctx["origin"])
            except Exception:
                logger.exception("on_resume_complete failed")

    def _record_approved_outcome(self, tool_name: str, result: ToolResult) -> None:
        """Tell the conversation an approved tool finished, so "did you do it?" is answered truthfully.

        The approval wait leaves "waiting for approval" as the last assistant turn; without this
        note the model keeps believing the action is still pending.
        """
        if result.success:
            out = result.output
            detail = (out.get("message") if isinstance(out, dict) else None) or (
                out if isinstance(out, str) else "")
            verb = "completed successfully"
        else:
            detail = result.error or "unknown error"
            verb = "FAILED"
        detail = redact_secrets(" ".join(str(detail).split()))
        if len(detail) > APPROVED_OUTCOME_MAX_CHARS:
            detail = detail[:APPROVED_OUTCOME_MAX_CHARS].rstrip() + "…"
        note = f"[Approved action finished] {tool_name} {verb}." + (f" {detail}" if detail else "")
        try:
            self._remember("assistant", note)
        except Exception:
            logger.warning("Could not record approved outcome for %s", tool_name, exc_info=True)

    @staticmethod
    def approval_wait_message(origin: str, tool_name: str, summary: str, arabic: bool = False,
                              pin: bool = False) -> str:
        """User-facing text for a pending approval; depends on where the request came from."""
        secs = int(DEFAULT_TTL)
        if pin and origin == CHANNEL_TELEGRAM:
            where_en, where_ar = "reply here with your PIN (or send cancel)", "أرسل الرمز السري هنا (أو أرسل إلغاء)"
        elif pin:
            where_en, where_ar = "enter your PIN in the HUD approvals panel", "أدخل الرمز السري في لوحة الموافقات في الواجهة"
        elif origin == CHANNEL_TELEGRAM:
            where_en, where_ar = "tap Approve or Deny on the Telegram message", "اضغط موافقة أو رفض في رسالة تيليجرام"
        elif origin == CHANNEL_VOICE:
            # Voice can never approve: say so out loud and point at the screen.
            where_en, where_ar = "please approve it on screen in the HUD, not by voice", "يرجى الموافقة على الشاشة في الواجهة، وليس بالصوت"
        else:
            where_en, where_ar = "approve or deny it in the HUD approvals panel", "وافق أو ارفض من لوحة الموافقات في الواجهة"
        if arabic:
            return f"🔐 بانتظار موافقتك على الشاشة لتنفيذ {tool_name}: {summary}. {where_ar} (تنتهي خلال {secs} ثانية)."
        return f"🔐 Waiting for your approval on screen to run {tool_name}: {summary}. To continue, {where_en} (expires in {secs}s)."

    def _request_approval(self, tool_name: str, tool_input: Dict[str, Any], result: ToolResult,
                          user_message: str = "") -> str:
        """Turn a gated ToolResult into a visible message (never drop it silently).

        With an ApprovalManager attached this creates a normal approval request
        bound to the exact args the handler saw; without one, it explains that
        the action was not run.
        """
        summary = result.permission_message or f"Run {tool_name}"
        arabic = any('؀' <= c <= 'ۿ' for c in (user_message or ""))
        self._pending_permission = None
        if self._approvals is None:
            return (f"⚠️ يتطلب هذا الإجراء موافقة ولم يتم تنفيذه: {summary}" if arabic
                    else f"⚠️ This action needs approval and was not run: {summary}")
        self._approvals.create_request(
            tool_name, strip_internal_keys(tool_input), channel=self._origin, summary=summary
        )
        update_hud_status(log_entry=f"Approval pending: {tool_name} — {summary}")
        return self.approval_wait_message(self._origin, tool_name, summary, arabic,
                                          pin=self._approvals.pin_required)

    def _approval_response(self, tool_name: str, tool_input: Dict[str, Any], result: ToolResult,
                           user_message: str, tools_used: List[str],
                           tool_outcomes: Optional[List[str]] = None, more_steps: bool = False) -> AgentResponse:
        """End the turn with a pending-approval message (persisted like any reply).

        ``more_steps`` (other tools already ran, are queued, or the request looks compound) makes
        run_approved resume the task once the approval is executed.
        """
        content = self._request_approval(tool_name, tool_input, result, user_message)
        self._resume_ctx = (
            {"user_message": user_message, "origin": self._origin, "done": list(tool_outcomes or [])}
            if more_steps and self._approvals is not None else None
        )
        self._messages.append(Message(role="assistant", content=content, timestamp=time.time()))
        self._remember("assistant", content, tool_outcomes)
        if self._database and self._conversation_id:
            self._database.add_message(self._conversation_id, "assistant", content)
        self._state = AgentState.IDLE
        return AgentResponse(
            content=content,
            steps=self._steps,
            tools_used=tools_used,
            success=False,
            error="awaiting approval",
        )

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
            return self._run_tool(tool_name, tool, tool_input, True)
        elif self._gate.get_permission_level(tool_name) == PermissionLevel.DENY:
            return ToolResult(
                success=False,
                output=None,
                error=f"Tool '{tool_name}' is blocked by session settings"
            )

        # Check database for saved permission
        if self._database:
            saved_permission = self._database.get_tool_permission(tool_name)
            if saved_permission == "allow" and not self._gate.refuses_blanket_allow(tool_name):
                return self._run_tool(tool_name, tool, tool_input, True)
            elif saved_permission == "deny":
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Tool '{tool_name}' is blocked by database permission settings"
                )

        # Tool requires permission
        if tool.requires_permission:
            result = self._run_tool(tool_name, tool, tool_input)

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

            return result  # no approval needed for this input; do not run the handler twice

        return self._run_tool(tool_name, tool, tool_input)

    def process(
        self,
        user_message: str,
        on_step: Optional[Callable[[AgentStep], None]] = None,
        on_permission: Optional[Callable[[PermissionRequest], None]] = None,
        on_complete: Optional[Callable[[AgentResponse], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        origin: str = CHANNEL_LOCAL,
        on_sentence: Optional[Callable[[str], None]] = None,
    ) -> Future:
        """Process a user message asynchronously.

        ``origin`` is the channel the message came from (local / hud / voice /
        telegram); it decides where approval requests are delivered.
        ``on_sentence`` enables streaming: complete sentences of the model's text
        are passed to it as they arrive (tool turns and the permission gate are unchanged).
        """
        future: Future = Future()
        self._cancel_flag = False
        self._state = AgentState.THINKING
        self._steps = []

        def _worker():
            reminder_timer: Optional[threading.Timer] = None
            try:
                def _announce_long_task_reminder():
                    reminder_text = "Sir, this may take a moment. Perhaps you should step away from the keyboard and take a short break."
                    print(f"\n[Altron Reminder] {reminder_text}\n")
                    if on_step:
                        try:
                            on_step(AgentStep(
                                step_type="thinking",
                                content=f"⏳ {reminder_text}"
                            ))
                        except Exception:
                            logger.debug("ignored error", exc_info=True)
                    try:
                        from src.services.voice_service import TTSManager
                        tts = TTSManager(enabled=True)
                        tts.speak(reminder_text)
                    except Exception as tts_err:
                        print(f"[Altron Reminder TTS Error] {tts_err}")

                reminder_timer = threading.Timer(60.0, _announce_long_task_reminder)
                reminder_timer.daemon = True
                reminder_timer.start()

                response = self._process_sync(
                    user_message,
                    on_step=on_step,
                    on_permission=on_permission,
                    origin=origin,
                    on_sentence=on_sentence,
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
                        logger.debug("ignored error", exc_info=True)
            finally:
                if reminder_timer is not None:
                    try:
                        reminder_timer.cancel()
                    except Exception:
                        logger.debug("ignored error", exc_info=True)
                # Always reset state to IDLE so subsequent messages can be processed
                self._state = AgentState.IDLE

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        return future

    def _process_sync(
        self,
        user_message: str,
        on_step: Optional[Callable[[AgentStep], None]] = None,
        on_permission: Optional[Callable[[PermissionRequest], None]] = None,
        origin: str = CHANNEL_LOCAL,
        on_sentence: Optional[Callable[[str], None]] = None,
        resume_prompt: Optional[str] = None,
    ) -> AgentResponse:
        """Synchronous processing logic with fast local intent bypass.

        ``resume_prompt`` continues a task paused for approval: the user turn is not re-recorded.
        """
        self._origin = origin
        self._tool_failures = {}
        self._turn_tainted = False
        # Clean up any temporary inspect_wa.ps1 if present in root directory
        try:
            wa_script = Path("inspect_wa.ps1")
            if wa_script.exists():
                wa_script.unlink()
        except Exception:
            logger.debug("ignored error", exc_info=True)

        tools_used = []
        tool_outcomes: List[str] = []

        # Automatically store new user message in short-term conversation memory and update HUD
        if resume_prompt is None:
            self._remember("user", user_message)
        update_hud_status(state="Thinking")

        # Include last 10 messages from memory.json as conversation context
        recent = self._memory.get_recent_messages(limit=10)
        self._messages = [
            Message(role=m["role"], content=m["content"], timestamp=time.time())
            for m in recent
        ]

        if resume_prompt is not None:
            self._messages.append(Message(role="user", content=resume_prompt, timestamp=time.time()))
        elif self._database and self._conversation_id:
            self._database.add_message(
                self._conversation_id,
                "user",
                user_message
            )

        turn_idx = len(self._messages) - 1
        mem_block = ""
        if resume_prompt is None:
            try:
                mem_block = memory_store.format_block(memory_store.get_store().search(user_message))
            except Exception:
                logger.debug("memory retrieval failed", exc_info=True)
            try:
                mem_block += scheduler.format_block(scheduler.get_scheduler().list_pending(limit=5))
            except Exception:
                logger.debug("scheduler context failed", exc_info=True)

        # Fast Local Intent Router (Bypass LLM for instant system controls and YouTube playback)
        # Compound requests go to the LLM: a local intent would run only one of the steps.
        local_intent = None if resume_prompt or _is_compound(user_message) else match_local_intent(user_message)
        if local_intent:
            tool_name = local_intent["tool_name"]
            tool_input = local_intent["tool_input"]
            confirmation = local_intent["confirmation"]

            self._state = AgentState.EXECUTING
            if tool_name is None:
                # Pure local answer (time/date): no tool, no API call.
                result = ToolResult(success=True, output={"message": confirmation})
            else:
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

                if result.requires_permission:
                    # Gated input (app_close, power shutdown/restart, app_open with a path): ask, never drop silently.
                    return self._approval_response(tool_name, tool_input, result, user_message, tools_used)

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
                tool_outcomes.append(f"{tool_name} {'ok' if result.success else 'failed'}")
            render = local_intent.get("render")
            if result.success and render:
                final_content = render(result.output)
            elif result.success and isinstance(result.output, dict) and result.output.get("message"):
                final_content = result.output["message"]
            else:
                final_content = confirmation if result.success else f"Action failed: {result.error}"

            final_content = sanitize_final_reply(final_content, user_message)

            assistant_msg = Message(role="assistant", content=final_content, timestamp=time.time())
            self._messages.append(assistant_msg)
            self._remember("assistant", final_content, tool_outcomes)

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
        streamed_speech = False

        while iteration < self._max_iterations and not self._cancel_flag:
            iteration += 1
            self._state = AgentState.THINKING

            messages = [m.to_api_format() for m in self._messages]
            # Memory goes into this turn's user message only (a copy): the system prompt and cached history stay byte-identical.
            if mem_block and iteration == 1 and 0 <= turn_idx < len(messages) and isinstance(messages[turn_idx].get("content"), str):
                messages[turn_idx] = {**messages[turn_idx], "content": mem_block + messages[turn_idx]["content"]}

            payload = {
                "model": self._ai_client._model,
                "max_tokens": self._ai_client._max_tokens,
                "system": self._get_system_prompt(),
                "messages": messages,
                "tools": self.tools_schema
            }

            thinking_step = AgentStep(step_type="thinking", content="Analyzing request...")
            self._steps.append(thinking_step)
            if on_step:
                on_step(thinking_step)

            streamer = SpeechStreamer(on_sentence) if on_sentence else None
            response_data = self._make_tool_request(
                payload, on_text=streamer.feed if streamer else None
            )
            if streamer:
                streamer.flush()

            if self._cancel_flag:
                break

            tool_uses = self._parse_tool_use(response_data)

            if not tool_uses:
                streamed_speech = bool(streamer and streamer.spoke)
                content = response_data.get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        final_content += block.get("text", "")

                # Detect plain-text tool call format: Using tool: <tool_name> with input: {"key": "value"}
                using_tool_match = re.search(r'Using tool:\s*([a-zA-Z0-9_]+)\s*with input:\s*(\{.*\})', final_content, re.DOTALL)
                if not using_tool_match:
                    using_tool_match = re.search(r'Using tool:\s*([a-zA-Z0-9_]+)\s*with input:\s*(\{.*?\})', final_content, re.DOTALL)

                if using_tool_match:
                    tool_name = using_tool_match.group(1).strip()
                    args_raw = using_tool_match.group(2).strip()
                    try:
                        try:
                            tool_input = json.loads(args_raw)
                        except Exception:
                            m_sub = re.search(r'(\{[\s\S]*?\})', args_raw)
                            tool_input = json.loads(m_sub.group(1)) if m_sub else {}

                        if self.registry.get(tool_name):
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

                            res = self.registry.execute(tool_name, **tool_input)
                            if res.requires_permission:
                                return self._approval_response(
                                    tool_name, tool_input, res, user_message, tools_used, tool_outcomes,
                                    more_steps=bool(tools_used) or _is_compound(user_message))
                            last_tool_result = res
                            tools_used.append(tool_name)
                            tool_outcomes.append(f"{tool_name} {'ok' if res.success else 'failed'}")

                            result_step = AgentStep(
                                step_type="tool_result",
                                content=f"Tool result: {res.output if res.success else res.error}",
                                tool_name=tool_name,
                                tool_result=res
                            )
                            self._steps.append(result_step)
                            if on_step:
                                on_step(result_step)

                            if tool_name in DIRECT_ACTION_TOOLS and res.success and not res.image and not _is_compound(user_message):
                                confirm_msg = ""
                                if isinstance(res.output, dict):
                                    confirm_msg = res.output.get("message") or res.output.get("summary") or ""
                                elif isinstance(res.output, str):
                                    confirm_msg = res.output

                                final_content = clean_tool_confirmation(
                                    confirm_msg,
                                    last_tool_name=tool_name,
                                    last_tool_input=tool_input,
                                    last_result=res,
                                    user_message=user_message
                                )
                                final_content = sanitize_final_reply(final_content, user_message)
                                assistant_msg = Message(role="assistant", content=final_content, timestamp=time.time())
                                self._messages.append(assistant_msg)
                                self._remember("assistant", final_content, tool_outcomes)
                                self._log_outcome(user_message, tool_outcomes)

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

                            # Feed tool result back to the LLM for final answer synthesis
                            result_repr = json.dumps(res.output, ensure_ascii=False) if res.success else f"Error: {res.error}"
                            self._messages.append(Message(
                                role="assistant",
                                content=f"Using tool: {tool_name} with input: {json.dumps(tool_input, ensure_ascii=False)}"
                            ))
                            self._append_tool_feedback(tool_name, result_repr, res)
                            final_content = ""
                            streamed_speech = False
                            continue
                    except Exception:
                        logger.debug("ignored error", exc_info=True)

                # Intercept raw JSON tool calls or raw Python code leaked as text
                intercepted = intercept_raw_code_or_payload(
                    text=final_content,
                    user_message=user_message,
                    executor=self
                )
                if intercepted:
                    intercepted_msg, intercepted_res = intercepted
                    final_content = intercepted_msg
                    if intercepted_res and intercepted_res.success:
                        tools_used.append("intercepted_action")
                        tool_outcomes.append("intercepted_action ok")
                elif tools_used:
                    final_content = clean_tool_confirmation(
                        final_content,
                        last_tool_name=last_tool_name,
                        last_tool_input=last_tool_input,
                        last_result=last_tool_result,
                        user_message=user_message
                    )

                final_content = sanitize_final_reply(final_content, user_message)

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

                if result.requires_permission and self._approvals is not None:
                    # Non-blocking: create an approval request and end the turn. The
                    # approved call later runs via run_approved with the stored args.
                    return self._approval_response(
                        tool_name, tool_input, result, user_message, tools_used, tool_outcomes,
                        more_steps=bool(tools_used) or tool_idx < len(tool_uses) - 1 or _is_compound(user_message))

                if result.requires_permission and self._pending_permission:
                    # Legacy in-process prompt (desktop chat window, no ApprovalManager).
                    approved = self._pending_permission.wait(timeout=DEFAULT_TTL)

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

                        result = self._execute_tool(tool_name, tool_input, approved=True)

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
                tool_outcomes.append(f"{tool_name} {'ok' if result.success else 'failed'}")

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
                self._append_tool_feedback(tool_name, result_repr, result)

                # Direct action tool fast-return: bypass second LLM summarization call
                # (never for a screenshot: the model has to look at the image)
                remaining_in_turn = len(tool_uses) - 1 - tool_idx
                if tool_name in DIRECT_ACTION_TOOLS and result.success and remaining_in_turn == 0 and not result.image and not _is_compound(user_message):
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

                    final_content = sanitize_final_reply(final_content, user_message)
                    self._remember("assistant", final_content, tool_outcomes)
                    self._log_outcome(user_message, tool_outcomes)
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

        final_content = sanitize_final_reply(final_content, user_message)

        default_fallback = "✅ تم إكمال العملية المطلوبة بنجاح!" if any('؀' <= char <= 'ۿ' for char in user_message) else "✅ I've completed the requested actions."
        resp_text = final_content or default_fallback
        self._remember("assistant", resp_text, tool_outcomes)
        self._log_outcome(user_message, tool_outcomes)
        self._state = AgentState.IDLE

        return AgentResponse(
            content=resp_text,
            steps=self._steps,
            tools_used=tools_used,
            success=True,
            streamed_speech=bool(streamed_speech and final_content),
        )

    def _make_tool_request(self, payload: Dict[str, Any], max_attempts: int = 3, backoff: float = 1.5,
                           on_text: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        """Make a tool-enabled API request with automatic retry via Universal AIClient.

        With ``on_text`` the request is streamed and text deltas are passed to it; the
        returned dict has the same shape either way.
        """
        if on_text is not None and hasattr(self._ai_client, "stream_tool_request"):
            return self._ai_client.stream_tool_request(
                payload, on_text=on_text, max_attempts=max_attempts, backoff=backoff
            )
        if hasattr(self._ai_client, "make_tool_request"):
            return self._ai_client.make_tool_request(payload, max_attempts=max_attempts, backoff=backoff)

        import urllib.request
        import urllib.error
        import socket
        import json as json_module

        url = f"{self._ai_client._base_url}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": getattr(self._ai_client, "_api_key", ""),
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
        self._memory.clear()
        self._history.clear()
        self._previous_context = ""
        try:
            memory_store.get_store().clear()  # "forget everything" also drops long-term memories
        except Exception:
            logger.debug("memory clear failed", exc_info=True)
