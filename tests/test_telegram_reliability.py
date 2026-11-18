"""Telegram offset persistence and hardened file uploads."""
from unittest import mock

import pytest

from src.services import telegram_service as tg

TOKEN = "123456789:AAEhBP0av28vWbRYVqzcAbCdEfGhIjKlMnO"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path))
    return tmp_path


def test_offset_roundtrip_and_default():
    assert tg.load_update_offset(TOKEN) == 0
    tg.save_update_offset(TOKEN, 42)
    assert tg.load_update_offset(TOKEN) == 42


def test_offset_ignored_for_other_bot_and_corrupt(data_dir):
    tg.save_update_offset(TOKEN, 42)
    assert tg.load_update_offset("999:other") == 0
    (data_dir / "telegram_offset.json").write_text("{bad", encoding="utf-8")
    assert tg.load_update_offset(TOKEN) == 0


def test_offset_file_has_no_token(data_dir):
    tg.save_update_offset(TOKEN, 7)
    assert TOKEN not in (data_dir / "telegram_offset.json").read_text(encoding="utf-8")


def test_worker_resumes_from_persisted_offset():
    tg.save_update_offset(TOKEN, 100)
    w = tg.TelegramRemoteWorker(bot_token=TOKEN, allowed_chat_id="1")
    assert w._offset == 100


def _resp(ok=True):
    r = mock.Mock()
    r.json.return_value = {"ok": ok}
    return r


def test_document_upload_streams_via_requests(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-1.4 hi")
    with mock.patch("requests.post", return_value=_resp()) as post:
        assert tg.send_telegram_document(str(f), caption="c", token=TOKEN, chat_id="5") is True
    (url,), kw = post.call_args
    assert url.endswith("/sendDocument")
    assert kw["data"] == {"chat_id": "5", "caption": "c"}
    assert "document" in kw["files"]


def test_photo_upload_sets_content_type(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"\x89PNG")
    with mock.patch("requests.post", return_value=_resp()) as post:
        assert tg.send_telegram_photo(str(f), token=TOKEN, chat_id="5") is True
    assert post.call_args.kwargs["files"]["photo"][2] == "image/png"


def test_oversize_file_rejected_before_open(tmp_path, monkeypatch):
    f = tmp_path / "big.pdf"
    f.write_bytes(b"x" * 20)
    monkeypatch.setattr(tg, "MAX_DOCUMENT_BYTES", 10)
    with mock.patch("requests.post") as post:
        assert tg.send_telegram_document(str(f), token=TOKEN, chat_id="5") is False
    post.assert_not_called()


def test_upload_error_is_redacted(tmp_path, capsys):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x")
    with mock.patch("requests.post", side_effect=OSError(f"boom /bot{TOKEN}/sendDocument")):
        assert tg.send_telegram_document(str(f), token=TOKEN, chat_id="5") is False
    assert TOKEN not in capsys.readouterr().out


def test_long_caption_truncated(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x")
    with mock.patch("requests.post", return_value=_resp()) as post:
        tg.send_telegram_document(str(f), caption="y" * 3000, token=TOKEN, chat_id="5")
    assert len(post.call_args.kwargs["data"]["caption"]) == 1024
