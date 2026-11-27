"""Email read requests must reach the LLM (Gmail tools), not the WhatsApp reader."""
import pytest

from src.agent.local_router import match_local_intent


@pytest.mark.parametrize("text", [
    "Read 5 messages from my email",
    "Read 5 messages my email",
    "read my gmail messages",
    "check my inbox messages",
    "اقرأ رسائل بريدي",
    "اقرأ 5 رسائل من الايميل",
])
def test_email_falls_through(text):
    assert match_local_intent(text) is None


@pytest.mark.parametrize("text", [
    "read whatsapp messages",
    "read whatsapp messages from John",
    "read reply from John",
])
def test_whatsapp_read_still_works(text):
    intent = match_local_intent(text)
    assert intent and intent["tool_name"] == "whatsapp_read"
