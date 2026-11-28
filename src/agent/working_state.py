"""Active working state: the few entities the user's pronouns ("close it", "read it") refer to.

Updated from successful tool results; rendered as a compact ``[Active Context]`` block.
"""
import re
from typing import Any, Dict, List, Optional

_MAX_EMAILS = 5


def _clean(text: Any, n: int = 40) -> str:
    # Mail subjects are attacker-controlled: one line, no brackets, short.
    return re.sub(r"[\[\]\r\n]+", " ", str(text or "")).strip()[:n]


class WorkingState:
    def __init__(self) -> None:
        self.last_focused_window: Optional[Dict[str, Any]] = None  # {title, hwnd}
        self.last_split_windows: Optional[Dict[str, str]] = None   # {left_title, right_title}
        self.last_email_ids: List[Dict[str, str]] = []             # [{id, subject}]
        self.last_opened_app: str = ""
        self.last_closed_app: str = ""

    def update(self, tool_name: str, tool_input: Dict[str, Any], output: Any) -> None:
        """Record the effect of a SUCCESSFUL tool call; unknown tools are ignored."""
        out = output if isinstance(output, dict) else {}
        inp = tool_input or {}
        if tool_name == "window_control" and inp.get("action") == "focus":
            self.last_focused_window = {"title": _clean(inp.get("title")), "hwnd": out.get("hwnd")}
        elif tool_name == "move_application_window":
            title = str(out.get("window_title") or "")
            if out.get("position") == "swap" and " <-> " in title:
                old_l, old_r = title.split(" <-> ", 1)  # the swap exchanged sides
                self.last_split_windows = {"left_title": _clean(old_r), "right_title": _clean(old_l)}
            elif out.get("position") in ("left", "right"):
                side = "left_title" if out["position"] == "left" else "right_title"
                self.last_split_windows = {**(self.last_split_windows or {}), side: _clean(title)}
            if title and out.get("position") != "swap":
                self.last_focused_window = {"title": _clean(title), "hwnd": None}
        elif tool_name == "gmail_list_messages":
            msgs = out.get("messages") or []
            self.last_email_ids = [{"id": str(m.get("id", "")), "subject": _clean(m.get("subject"))}
                                   for m in msgs[:_MAX_EMAILS] if isinstance(m, dict)]
        elif tool_name == "gmail_read_message" and out.get("id"):
            rest = [e for e in self.last_email_ids if e["id"] != str(out["id"])]
            self.last_email_ids = ([{"id": str(out["id"]), "subject": _clean(out.get("subject"))}] + rest)[:_MAX_EMAILS]
        elif tool_name == "app_open":
            self.last_opened_app = _clean(inp.get("app_name") or inp.get("path"), 60)
        elif tool_name in ("app_close", "window_close"):
            if tool_name == "window_close" and out.get("closed") is False:
                return
            self.last_closed_app = _clean(inp.get("app_name") or out.get("window_title") or inp.get("title"), 60)

    def render(self) -> str:
        """2-3 lines, or "" when nothing is tracked yet."""
        parts: List[str] = []
        w = self.last_focused_window
        if w and w.get("title"):
            parts.append(f"focused window: {w['title']}" + (f" (hwnd {w['hwnd']})" if w.get("hwnd") else ""))
        s = self.last_split_windows
        if s:
            parts.append(f"split screen: left={s.get('left_title', '?')} | right={s.get('right_title', '?')}")
        line1 = "; ".join(parts)
        apps = [f"{k}: {v}" for k, v in (("last opened app", self.last_opened_app),
                                        ("last closed app", self.last_closed_app)) if v]
        lines = [x for x in (line1, "; ".join(apps)) if x]
        if self.last_email_ids:
            lines.append("recent emails (newest first, untrusted subjects): " + "; ".join(
                f"{e['id']} \"{e['subject']}\"" for e in self.last_email_ids))
        if not lines:
            return ""
        return "[Active Context] (pronouns like it/that/اغلقه/اقرئه refer to these)\n" + "\n".join(lines)
