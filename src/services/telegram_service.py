"""Telegram integration service for AI Desktop Assistant.

Provides two-way remote control over Telegram long-polling and delivery functions.
"""
import os
import json
import threading
import urllib.request
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal
from src.config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

DEFAULT_BOT_TOKEN = TELEGRAM_BOT_TOKEN
DEFAULT_CHAT_ID = TELEGRAM_CHAT_ID


def chunk_message(text: str, max_length: int = 4000) -> list[str]:
    """Split long text into safe chunks under Telegram's 4096-character limit."""
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Find best split point near max_length (double newline, single newline, period, space)
        split_idx = remaining.rfind("\n\n", 0, max_length)
        if split_idx == -1 or split_idx < max_length // 2:
            split_idx = remaining.rfind("\n", 0, max_length)
        if split_idx == -1 or split_idx < max_length // 2:
            split_idx = remaining.rfind(". ", 0, max_length)
            if split_idx != -1:
                split_idx += 1  # include period
        if split_idx == -1 or split_idx < max_length // 2:
            split_idx = remaining.rfind(" ", 0, max_length)
        if split_idx == -1:
            split_idx = max_length

        chunk = remaining[:split_idx].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_idx:].strip()

    return chunks


def send_telegram_message(text: str, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    """Send text message to Telegram chat via Bot API, automatically splitting long messages into safe chunks."""
    bot_token = token or os.environ.get("TELEGRAM_BOT_TOKEN") or DEFAULT_BOT_TOKEN
    target_chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID") or DEFAULT_CHAT_ID

    if not bot_token or not target_chat_id:
        try:
            from src.config.settings import load_env_file
            env_path = Path(__file__).resolve().parent.parent.parent / ".env"
            if env_path.exists():
                env_vars = load_env_file(env_path)
                bot_token = bot_token or env_vars.get("TELEGRAM_BOT_TOKEN")
                target_chat_id = target_chat_id or env_vars.get("TELEGRAM_CHAT_ID")
        except Exception:
            pass

    if not bot_token or not target_chat_id or not text or not str(text).strip():
        return False

    chunks = chunk_message(str(text).strip(), max_length=4000)
    all_ok = True

    for chunk in chunks:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": str(target_chat_id),
            "text": chunk
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                res_json = json.loads(resp.read().decode("utf-8"))
                if not res_json.get("ok"):
                    all_ok = False
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            print(f"[Telegram Service] HTTPError {e.code}: {err_body}")
            all_ok = False
        except Exception as e:
            print(f"[Telegram Service] Send error: {e}")
            all_ok = False

    return all_ok


def send_telegram_message_async(text: str, token: Optional[str] = None, chat_id: Optional[str] = None) -> Optional[threading.Thread]:
    """Send text message to Telegram chat in a background daemon thread without blocking UI."""
    if not text or not str(text).strip():
        return None

    thread = threading.Thread(
        target=send_telegram_message,
        args=(text,),
        kwargs={"token": token, "chat_id": chat_id},
        daemon=True
    )
    thread.start()
    return thread


def send_telegram_document(
    file_path: str,
    caption: str = "",
    token: Optional[str] = None,
    chat_id: Optional[str] = None
) -> bool:
    """Send a local file (e.g. PDF) to Telegram chat using multipart/form-data."""
    bot_token = token or os.environ.get("TELEGRAM_BOT_TOKEN") or DEFAULT_BOT_TOKEN
    target_chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID") or DEFAULT_CHAT_ID

    path = Path(file_path).expanduser().resolve()
    if not path.is_file() or not bot_token or not target_chat_id:
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"

    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    body = bytearray()

    # chat_id field
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'.encode("utf-8"))
    body.extend(f"{target_chat_id}\r\n".encode("utf-8"))

    # caption field (Telegram caption limit is 1024 characters)
    if caption:
        safe_caption = caption[:995] + "..." if len(caption) > 1000 else caption
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="caption"\r\n\r\n'.encode("utf-8"))
        body.extend(f"{safe_caption}\r\n".encode("utf-8"))

    # document file
    filename = path.name
    with open(path, "rb") as f:
        file_bytes = f.read()

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode("utf-8"))
    body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
    body.extend(file_bytes)
    body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body))
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            return bool(res_json.get("ok"))
    except Exception:
        return False


def send_telegram_photo(
    image_path: str,
    caption: str = "",
    token: Optional[str] = None,
    chat_id: Optional[str] = None
) -> bool:
    """Send an image photo to Telegram chat via Bot API sendPhoto using multipart/form-data."""
    bot_token = token or os.environ.get("TELEGRAM_BOT_TOKEN") or DEFAULT_BOT_TOKEN
    target_chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID") or DEFAULT_CHAT_ID

    path = Path(image_path).expanduser().resolve()
    if not path.is_file() or not bot_token or not target_chat_id:
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"

    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    body = bytearray()

    # chat_id field
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'.encode("utf-8"))
    body.extend(f"{target_chat_id}\r\n".encode("utf-8"))

    # caption field (Telegram caption limit is 1024 characters)
    if caption:
        safe_caption = caption[:995] + "..." if len(caption) > 1000 else caption
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="caption"\r\n\r\n'.encode("utf-8"))
        body.extend(f"{safe_caption}\r\n".encode("utf-8"))

    # photo file field
    filename = path.name
    with open(path, "rb") as f:
        file_bytes = f.read()

    ext = path.suffix.lower()
    content_type = "image/png" if ext == ".png" else "image/jpeg" if ext in (".jpg", ".jpeg") else "application/octet-stream"

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode("utf-8"))
    body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body))
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            return bool(res_json.get("ok"))
    except Exception:
        return False


def send_telegram_photo_async(
    image_path: str,
    caption: str = "",
    token: Optional[str] = None,
    chat_id: Optional[str] = None
) -> Optional[threading.Thread]:
    """Send photo to Telegram chat in a background daemon thread without blocking UI."""
    if not image_path:
        return None

    thread = threading.Thread(
        target=send_telegram_photo,
        args=(image_path,),
        kwargs={"caption": caption, "token": token, "chat_id": chat_id},
        daemon=True
    )
    thread.start()
    return thread


class TelegramRemoteWorker(QThread):
    """Background worker that long-polls Telegram getUpdates for phone remote control."""

    remote_command_received = Signal(str)

    def __init__(self, bot_token: Optional[str] = None, allowed_chat_id: Optional[str] = None, parent=None):
        super().__init__(parent)
        self._token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN") or DEFAULT_BOT_TOKEN
        self._allowed_chat_id = str(allowed_chat_id or os.environ.get("TELEGRAM_CHAT_ID") or DEFAULT_CHAT_ID)
        self._is_running = True
        self._offset = 0

    def stop(self) -> None:
        """Cleanly stop long polling."""
        self._is_running = False

    def run(self) -> None:
        """Poll getUpdates and emit remote_command_received for authorized messages."""
        if not self._token or not self._allowed_chat_id:
            print("[Telegram Worker] Error: Missing bot token or allowed chat ID, polling aborted.")
            return

        print(f"[Telegram Worker] Started polling Telegram getUpdates for chat_id: {self._allowed_chat_id}")

        while self._is_running:
            try:
                poll_url = (
                    f"https://api.telegram.org/bot{self._token}/getUpdates"
                    f"?offset={self._offset}&timeout=15"
                )
                req = urllib.request.Request(poll_url)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                if not data.get("ok"):
                    self.msleep(1000)
                    continue

                for update in data.get("result", []):
                    if not self._is_running:
                        break

                    update_id = update.get("update_id", 0)
                    if update_id >= self._offset:
                        self._offset = update_id + 1

                    message = update.get("message") or update.get("channel_post")
                    if not message:
                        continue

                    from_chat = message.get("chat", {})
                    chat_id_str = str(from_chat.get("id", ""))

                    # Strictly enforce authorization check
                    if chat_id_str != self._allowed_chat_id:
                        print(f"[Telegram Worker] Unauthorized message received from chat_id {chat_id_str} (expected {self._allowed_chat_id})")
                        continue

                    text = message.get("text", "").strip()
                    if text:
                        print(f"[Telegram Worker] Incoming remote command from Telegram: {text}")
                        self.remote_command_received.emit(text)

            except Exception as e:
                # On network glitch or timeout, back off briefly
                print(f"[Telegram Worker] Polling error: {e}")
                self.msleep(2000)
