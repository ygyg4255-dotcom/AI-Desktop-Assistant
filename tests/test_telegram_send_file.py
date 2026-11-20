"""Tests for send_file / telegram_send_file (no network, no real credentials)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.services import telegram_service as ts


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home / "Desktop"


def test_relative_path_resolves_to_desktop(desktop, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (desktop / "deck.deckcraft").write_bytes(b"x")
    assert ts.resolve_send_path("deck.deckcraft") == desktop / "deck.deckcraft"


def test_cwd_file_wins_over_desktop(desktop, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.pdf").write_bytes(b"x")
    (desktop / "a.pdf").write_bytes(b"y")
    assert ts.resolve_send_path("a.pdf").resolve() == (tmp_path / "a.pdf").resolve()


def test_missing_file(desktop):
    ok, msg = ts.send_file("nope.docx", token="t", chat_id="1")
    assert not ok and "not found" in msg.lower()


def test_not_configured(desktop, monkeypatch):
    monkeypatch.setattr(ts, "get_config_value", lambda *a, **k: "")
    (desktop / "a.pdf").write_bytes(b"x")
    ok, msg = ts.send_file("a.pdf")
    assert not ok and "not configured" in msg.lower()


def test_over_50mb_rejected(desktop, monkeypatch):
    f = desktop / "big.xlsx"
    f.write_bytes(b"x")
    monkeypatch.setattr(ts, "MAX_DOCUMENT_BYTES", 0)
    with patch("requests.post") as post:
        ok, msg = ts.send_file("big.xlsx", token="t", chat_id="1")
    assert not ok and "limit" in msg
    post.assert_not_called()


def test_send_document_upload(desktop):
    (desktop / "r.pdf").write_bytes(b"%PDF")
    resp = MagicMock()
    resp.json.return_value = {"ok": True}
    with patch("requests.post", return_value=resp) as post:
        ok, msg = ts.send_file("r.pdf", caption="hi", token="TOK", chat_id="42")
    assert ok and msg == "r.pdf"
    args, kwargs = post.call_args
    assert args[0].endswith("/botTOK/sendDocument")
    assert kwargs["data"] == {"chat_id": "42", "caption": "hi"}
    assert "document" in kwargs["files"]


def test_telegram_rejects(desktop):
    (desktop / "r.pdf").write_bytes(b"x")
    resp = MagicMock()
    resp.json.return_value = {"ok": False}
    with patch("requests.post", return_value=resp):
        ok, msg = ts.send_file("r.pdf", token="t", chat_id="1")
    assert not ok


def test_tool_registered_and_safe():
    from src.agent.permissions import PermissionGate
    from src.agent.tools import create_default_registry
    assert create_default_registry().get("telegram_send_file") is not None
    assert "telegram_send_file" in PermissionGate.SAFE_TOOLS


def test_tool_handler_error_and_success():
    from src.agent.tools import handle_telegram_send_file
    with patch("src.services.telegram_service.send_file", return_value=(False, "File not found: x")):
        r = handle_telegram_send_file({"path": "x"})
    assert not r.success and "not found" in r.error
    with patch("src.services.telegram_service.send_file", return_value=(True, "x.pdf")):
        r = handle_telegram_send_file({"path": "x.pdf"})
    assert r.success and "x.pdf" in r.output["message"]
