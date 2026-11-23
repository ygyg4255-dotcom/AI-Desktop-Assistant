"""Honest tool-result feedback and action tags for the agent loop.

The tag is recorded by the executor (never written by the model) so later turns can
tell which assistant replies were backed by a real tool run.
"""
from typing import Iterable

NL = chr(10)
ACTION_TAG_PREFIX = "[Actions run this turn:"
MAX_TOOL_FAILURES = 2  # failures per tool per turn before the model must stop retrying


def tool_result_prompt(tool_name: str, result_repr: str, success: bool, failures: int = 0) -> str:
    """User-side message handing a tool's real result back to the model."""
    head = f"Tool '{tool_name}' execution result:{NL}{result_repr}{NL}{NL}"
    if success:
        return head + (
            "Reply to the user in their language (Arabic or English) in one or two natural sentences. "
            "Report ONLY what the result above shows; do not claim any action that is not in it. "
            "Never output raw JSON, '[Tool Result]' or 'Using tool:' blocks."
        )
    if failures < MAX_TOOL_FAILURES:
        return head + (
            "This tool call FAILED. If a clearly different approach could work (other arguments or another tool), "
            "try ONE now; otherwise tell the user briefly, in their language, that it did not work and why. "
            "Do NOT say or imply the action was completed. "
            "Never output raw JSON, '[Tool Result]' or 'Using tool:' blocks."
        )
    return head + (
        "This tool call FAILED. Tell the user briefly, in their language, that it did not work and why. "
        "Do NOT say or imply the action was completed. "
        "Never output raw JSON, '[Tool Result]' or 'Using tool:' blocks."
    )


def format_action_tag(outcomes: Iterable[str]) -> str:
    items = [o for o in outcomes if o]
    return f"{ACTION_TAG_PREFIX} {', '.join(items)}]" if items else ""


def tag_reply(content: str, outcomes: Iterable[str]) -> str:
    """Prefix a stored assistant reply with the tools that really ran this turn."""
    tag = format_action_tag(outcomes)
    return f"{tag} {content}" if tag else content


def strip_action_tag(text: str) -> str:
    """Remove a leading action tag if the model echoed one into its reply."""
    stripped = (text or "").lstrip()
    if stripped.startswith(ACTION_TAG_PREFIX):
        end = stripped.find("]")
        if end != -1:
            return stripped[end + 1:].lstrip()
    return text
