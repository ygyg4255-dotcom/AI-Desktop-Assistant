"""'open <target>' fast-path only fires for known apps; everything else falls through to the LLM."""
import pytest

from src.agent.local_router import match_local_intent


@pytest.mark.parametrize("text,app", [
    ("open vscode", "vscode"),
    ("open chrome", "chrome"),
    ("open notepad", "notepad"),
])
def test_known_app_hits_fast_path(text, app):
    intent = match_local_intent(text)
    assert intent and intent["tool_name"] == "app_open"
    assert intent["tool_input"] == {"app_name": app}


@pytest.mark.parametrize("text", [
    "open him",
    "open Clipboard",
    "open him in vscode",
    "open my project",
])
def test_unknown_target_falls_through(text):
    assert match_local_intent(text) is None
