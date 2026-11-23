from types import SimpleNamespace

from main import is_duplicate_approval_reply


def test_telegram_gated_reply_suppressed_when_card_sent():
    res = SimpleNamespace(content="🔐 Waiting for your approval ...", error="awaiting approval")
    assert is_duplicate_approval_reply(res, object()) is True


def test_normal_reply_and_no_bridge_still_sent():
    ok = SimpleNamespace(content="done", error=None)
    gated = SimpleNamespace(content="🔐 Waiting ...", error="awaiting approval")
    assert is_duplicate_approval_reply(ok, object()) is False
    assert is_duplicate_approval_reply(gated, None) is False  # no card was sent, keep the text
