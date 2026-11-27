"""Mute fast-path only fires for explicit system-audio commands."""
import pytest

from src.agent.local_router import match_local_intent


@pytest.mark.parametrize("text", [
    "قم بكتم الرسائل من اولاما",
    "mute messages from ollama",
    "mute notifications",
    "كتم الرسائل من احمد",
])
def test_mute_other_target_falls_through(text):
    assert match_local_intent(text) is None


@pytest.mark.parametrize("text", ["اكتم الصوت", "كتم الصوت", "شغل الصوت", "mute volume", "mute", "unmute", "mute sound"])
def test_mute_audio_still_works(text):
    intent = match_local_intent(text)
    assert intent and intent["tool_input"]["action"] == "volume_mute"
