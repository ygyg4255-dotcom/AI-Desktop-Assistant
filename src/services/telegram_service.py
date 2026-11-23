"""Telegram integration service for AI Desktop Assistant.

Provides two-way remote control over Telegram long-polling and delivery functions.
"""
import os
import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QThread, Signal
from src.config.paths import runtime_file
from src.config.log import get_logger, redact_secrets  # redact_secrets re-exported (audit S6)
from src.config.settings import get_config_value
from src.services.signals import CallbackSignal

logger = get_logger(__name__)

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


def resolve_credentials(token: Optional[str] = None, chat_id: Optional[str] = None) -> tuple[str, str]:
    """Bot token and chat id: explicit arguments, else the canonical settings lookup."""
    return (
        str(token or get_config_value("TELEGRAM_BOT_TOKEN")).strip(),
        str(chat_id or get_config_value("TELEGRAM_CHAT_ID")).strip(),
    )


def send_telegram_message(text: str, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    """Send text message to Telegram chat via Bot API, automatically splitting long messages into safe chunks."""
    bot_token, target_chat_id = resolve_credentials(token, chat_id)

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
            logger.warning("sendMessage HTTPError %s: %s", e.code, redact_secrets(err_body, bot_token))
            all_ok = False
        except Exception as e:
            logger.warning("sendMessage failed: %s", redact_secrets(e, bot_token))
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


# Bot API upload limits (sendDocument 50 MB, sendPhoto 10 MB).
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
_CAPTION_LIMIT = 1024


def _post_file(
    method: str,
    field: str,
    file_path: str,
    caption: str,
    token: Optional[str],
    chat_id: Optional[str],
    max_bytes: int,
    content_type: Optional[str] = None,
) -> bool:
    """Upload a local file with a streamed multipart POST (never reads it into memory)."""
    import requests

    bot_token, target_chat_id = resolve_credentials(token, chat_id)

    path = Path(file_path).expanduser().resolve()
    if not path.is_file() or not bot_token or not target_chat_id:
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size > max_bytes:
        logger.warning("%s: file too large (%d > %d bytes), not sent", method, size, max_bytes)
        return False

    data = {"chat_id": str(target_chat_id)}
    if caption:
        data["caption"] = caption if len(caption) <= _CAPTION_LIMIT else caption[:_CAPTION_LIMIT - 3] + "..."

    try:
        with open(path, "rb") as fh:
            file_tuple = (path.name, fh, content_type) if content_type else (path.name, fh)
            resp = requests.post(
                f"https://api.telegram.org/bot{bot_token}/{method}",
                data=data,
                files={field: file_tuple},
                timeout=30,
            )
        return bool(resp.json().get("ok"))
    except Exception as err:
        logger.warning("%s failed: %s", method, redact_secrets(err, bot_token))
        return False


def send_telegram_document(
    file_path: str,
    caption: str = "",
    token: Optional[str] = None,
    chat_id: Optional[str] = None
) -> bool:
    """Send a local file (e.g. PDF) to the Telegram chat (max 50 MB)."""
    return _post_file("sendDocument", "document", file_path, caption, token, chat_id, MAX_DOCUMENT_BYTES)


def resolve_send_path(file_path: str) -> Path:
    """Expand a user path; a relative path missing from the cwd falls back to the Desktop."""
    path = Path(str(file_path).strip().strip('"')).expanduser()
    if path.is_absolute() or path.exists():
        return path
    return Path.home() / "Desktop" / path


def send_file(
    file_path: str,
    caption: Optional[str] = None,
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> tuple[bool, str]:
    """Send any local file to the configured chat via sendDocument.

    Returns (ok, message); message is the sent filename on success, else a clear error.
    """
    if not str(file_path or "").strip():
        return False, "No file path given."
    bot_token, target_chat_id = resolve_credentials(token, chat_id)
    if not bot_token or not target_chat_id:
        return False, "Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env."
    path = resolve_send_path(file_path)
    if not path.is_file():
        return False, f"File not found: {path}"
    try:
        size = path.stat().st_size
    except OSError as err:
        return False, f"Cannot read file '{path}': {err}"
    if size > MAX_DOCUMENT_BYTES:
        return False, (
            f"File '{path.name}' is {size / (1024 * 1024):.1f} MB, over Telegram's "
            f"{MAX_DOCUMENT_BYTES // (1024 * 1024)} MB bot upload limit."
        )
    if not _post_file("sendDocument", "document", str(path), caption or "",
                      bot_token, target_chat_id, MAX_DOCUMENT_BYTES):
        return False, f"Telegram rejected or failed to receive '{path.name}'."
    return True, path.name


def send_telegram_photo(
    image_path: str,
    caption: str = "",
    token: Optional[str] = None,
    chat_id: Optional[str] = None
) -> bool:
    """Send an image to the Telegram chat via sendPhoto (max 10 MB)."""
    ext = Path(image_path).suffix.lower()
    content_type = "image/png" if ext == ".png" else "image/jpeg" if ext in (".jpg", ".jpeg") else None
    return _post_file("sendPhoto", "photo", image_path, caption, token, chat_id, MAX_PHOTO_BYTES, content_type)


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


class TelegramBotApi:
    """Minimal JSON Bot API caller. Injectable so tests never touch the network.

    The token is part of the URL, so URLs are never logged.
    """

    def __init__(self, bot_token: str, timeout: float = 15.0):
        self._token = bot_token
        self._timeout = timeout

    def call(self, method: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as err:
            logger.warning("Bot API %s failed: %s", method, type(err).__name__)
            return {"ok": False}


CANCEL_WORDS = ("cancel", "إلغاء", "الغاء")

_STATUS_LABELS = {
    "approved": "✅ Approved",
    "denied": "❌ Denied",
    "expired": "⌛ Expired (denied)",
}


def _background(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="AltronTelegramApproval", daemon=True).start()


class TelegramApprovalBridge:
    """Delivers approval requests as inline keyboards and handles the callbacks.

    A callback is honoured only if the chat id AND the sender user id are the
    owner's, and the nonce + args-hash prefix match a live, unused request.
    Bot API replies are sent via ``dispatch`` (a background thread by default)
    so the polling listener never waits on them.
    """

    def __init__(self, manager, api, chat_id: str, owner_user_id: Optional[str] = None,
                 dispatch: Callable[[Callable[[], None]], None] = _background):
        self._manager = manager
        self._api = api
        self._chat_id = str(chat_id)
        # In a private chat with the bot, chat.id == the owner's user id.
        self._owner_user_id = str(owner_user_id or chat_id)
        self._dispatch = dispatch
        self._lock = threading.Lock()
        self._messages: dict = {}  # request id -> (message_id, text)
        self._pin_pending: list = []  # request ids awaiting a PIN reply, oldest first

    @staticmethod
    def request_text(req) -> str:
        # Plain-language summary only: raw args / code stay in the HUD's technical details.
        return (
            "🔐 Approval needed\n"
            f"Tool: {req.tool_name}\n"
            f"Action: {req.summary[:300]}\n"
            f"Requested via: {req.channel}\n"
            f"Expires in {int(req.expires_at - req.created_at)}s"
        )

    def send_request(self, req) -> None:
        text = self.request_text(req)
        h = req.args_hash[:16]
        if self._manager.pin_required:
            # PIN mode: no buttons. The owner's next message is the PIN (see handle_message).
            text += (
                "\n\n🔑 Reply with your PIN to execute, or send "
                f"\"cancel\" / \"{CANCEL_WORDS[1]}\" to abort."
            )
            with self._lock:
                self._pin_pending.append(req.id)
            markup = None
        else:
            markup = {"inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"ap:a:{req.id}:{h}"},
                {"text": "❌ Deny", "callback_data": f"ap:d:{req.id}:{h}"},
            ]]}

        def _send():
            payload = {"chat_id": self._chat_id, "text": text}
            if markup:
                payload["reply_markup"] = markup
            res = self._api.call("sendMessage", payload)
            msg_id = ((res or {}).get("result") or {}).get("message_id")
            if msg_id is not None:
                with self._lock:
                    self._messages[req.id] = (msg_id, text)

        self._dispatch(_send)

    def on_resolved(self, req) -> None:
        """Edit the original message to its final status (also for HUD/expiry)."""
        with self._lock:
            entry = self._messages.pop(req.id, None)
            if req.id in self._pin_pending:
                self._pin_pending.remove(req.id)
        label = _STATUS_LABELS.get(req.status)
        if not entry or not label:
            return
        msg_id, text = entry
        self._dispatch(lambda: self._api.call("editMessageText", {
            "chat_id": self._chat_id, "message_id": msg_id, "text": f"{text}\n\n{label}",
        }))

    def _say(self, text: str) -> None:
        self._dispatch(lambda: self._api.call("sendMessage", {"chat_id": self._chat_id, "text": text}))

    def handle_message(self, message: dict) -> bool:
        """Consume the owner's message as a PIN/cancel reply while a PIN request is pending.

        Returns True when the message was taken (it must then not be logged or sent to the
        LLM). The PIN text is never logged or echoed, and the message is deleted from the chat.
        """
        message = message or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        user_id = str((message.get("from") or {}).get("id", ""))
        if chat_id != self._chat_id or user_id != self._owner_user_id:
            return False
        with self._lock:
            pending = [r for r in self._pin_pending
                       if getattr(self._manager.get(r), "status", "") == "pending"]
            self._pin_pending[:] = pending
        if not pending or not self._manager.pin_required:
            return False
        rid, text = pending[-1], str(message.get("text", "")).strip()
        msg_id = message.get("message_id")
        if msg_id is not None:
            self._dispatch(lambda: self._api.call("deleteMessage", {"chat_id": self._chat_id, "message_id": msg_id}))

        if text.lower() in CANCEL_WORDS:
            self._manager.cancel(rid, "telegram")
            self._say("❌ Cancelled.")
            return True
        res = self._manager.verify_pin_and_approve(rid, text, "telegram")
        if res.outcome == "approved":
            self._say("✅ PIN accepted — running now.")
        elif res.outcome == "wrong_pin":
            self._say(f"⚠️ Wrong PIN. {res.attempts_left} attempt(s) left.")
        elif res.outcome == "locked":
            self._say("🔒 Too many wrong PINs. The request was denied.")
        else:
            self._say("This request is no longer pending.")
        return True

    def _answer(self, cq_id, text: str) -> None:
        if cq_id is not None:
            self._dispatch(lambda: self._api.call("answerCallbackQuery", {"callback_query_id": cq_id, "text": text}))

    def handle_callback_query(self, cq: dict) -> str:
        """Process one callback_query update. Returns the outcome string."""
        cq = cq or {}
        cq_id = cq.get("id")
        chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
        user_id = str((cq.get("from") or {}).get("id", ""))
        if chat_id != self._chat_id or user_id != self._owner_user_id:
            logger.warning("Rejected approval callback from unauthorized chat/user")
            self._answer(cq_id, "Not authorized.")
            return "forbidden"

        parts = str(cq.get("data", "")).split(":")
        if len(parts) != 4 or parts[0] != "ap" or parts[1] not in ("a", "d"):
            self._answer(cq_id, "Unknown action.")
            return "unknown"
        _, action, request_id, hash_prefix = parts

        result = self._manager.resolve(request_id, approve=(action == "a"), via="telegram",
                                       expected_hash=hash_prefix)
        replies = {
            "approved": "Approved — running now.",
            "denied": "Denied.",
            "expired": "This request has expired.",
            "already_used": "This request was already handled.",
            "hash_mismatch": "Request did not match; denied.",
            "forbidden_channel": "This request cannot be approved from Telegram.",
            "pin_required": "A PIN is required: reply to the request message with it.",
            "unknown": "Unknown or expired request.",
        }
        self._answer(cq_id, replies.get(result.outcome, "Not processed."))
        if result.outcome == "expired" and result.request is not None:
            self.on_resolved(result.request)  # make sure the message shows Expired
        return result.outcome


_OFFSET_FILE = "telegram_offset.json"


def _bot_id(token: Optional[str]) -> str:
    return (token or "").split(":", 1)[0]


def load_update_offset(token: Optional[str]) -> int:
    """Last persisted getUpdates offset for this bot (0 if none/corrupt/other bot)."""
    try:
        data = json.loads(runtime_file(_OFFSET_FILE).read_text(encoding="utf-8"))
        if data.get("bot_id") == _bot_id(token):
            return max(0, int(data.get("offset", 0)))
    except FileNotFoundError:
        pass
    except Exception as err:
        logger.warning("Ignoring unreadable Telegram offset file: %s", type(err).__name__)
    return 0


def save_update_offset(token: Optional[str], offset: int) -> None:
    """Atomically persist the getUpdates offset so a restart does not replay old messages."""
    target = runtime_file(_OFFSET_FILE)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"bot_id": _bot_id(token), "offset": int(offset)}), encoding="utf-8")
        os.replace(tmp, target)
    except Exception as err:
        logger.warning("Could not persist Telegram update offset: %s", type(err).__name__)


class TelegramRemoteListener:
    """Long-polls Telegram getUpdates for phone remote control (Qt-free).

    The headless runtime (main.py) calls ``run()`` on a plain ``threading.Thread``;
    ``remote_command_received`` is a ``CallbackSignal`` invoked on that thread.
    """

    remote_command_received = CallbackSignal()

    def __init__(self, bot_token: Optional[str] = None, allowed_chat_id: Optional[str] = None,
                 callback_handler: Optional[Callable[[dict], object]] = None,
                 message_interceptor: Optional[Callable[[dict], bool]] = None):
        self._token, self._allowed_chat_id = resolve_credentials(bot_token, allowed_chat_id)
        self._is_running = True
        self._offset = load_update_offset(self._token)
        # Handles inline-keyboard callback_query updates (approval buttons). It must
        # return quickly; the approval bridge does its network I/O off this thread.
        self._callback_handler = callback_handler
        # Gets each authorized message first; True means "consumed" (e.g. a PIN reply), so it
        # is neither logged nor emitted as a command.
        self._message_interceptor = message_interceptor

    def stop(self) -> None:
        """Cleanly stop long polling."""
        self._is_running = False

    def run(self) -> None:
        """Poll getUpdates and emit remote_command_received for authorized messages."""
        if not self._token or not self._allowed_chat_id:
            logger.error("Missing bot token or allowed chat ID; Telegram polling aborted")
            return

        logger.info("Started polling Telegram getUpdates for chat_id %s", self._allowed_chat_id)

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
                    time.sleep(1.0)
                    continue

                for update in data.get("result", []):
                    if not self._is_running:
                        break

                    update_id = update.get("update_id", 0)
                    if update_id >= self._offset:
                        self._offset = update_id + 1
                        # Persist before handling: at-most-once, so a restart never replays commands.
                        save_update_offset(self._token, self._offset)

                    callback_query = update.get("callback_query")
                    if callback_query is not None:
                        if self._callback_handler is not None:
                            try:
                                self._callback_handler(callback_query)
                            except Exception as cb_err:
                                logger.error("Callback handling error: %s", redact_secrets(cb_err, self._token))
                        continue

                    message = update.get("message") or update.get("channel_post")
                    if not message:
                        continue

                    from_chat = message.get("chat", {})
                    chat_id_str = str(from_chat.get("id", ""))

                    # Strictly enforce authorization check
                    if chat_id_str != self._allowed_chat_id:
                        logger.warning("Ignored message from unauthorized chat_id %s", chat_id_str)
                        continue

                    if self._message_interceptor is not None:
                        try:
                            if self._message_interceptor(message):
                                continue
                        except Exception as ic_err:  # a broken interceptor must never swallow normal commands
                            logger.error("Message interceptor error: %s", redact_secrets(ic_err, self._token))

                    text = message.get("text", "").strip()
                    if text:
                        logger.info("Incoming remote command from Telegram: %s", text)
                        self.remote_command_received.emit(text)

            except Exception as e:
                # On network glitch or timeout, back off briefly
                logger.warning("Telegram polling error: %s", redact_secrets(e, self._token))
                time.sleep(2.0)


class TelegramRemoteWorker(QThread, TelegramRemoteListener):
    """Legacy Qt wrapper (deprecated src/ui/ only): same loop, a real Qt signal.

    Start it with ``start()``; never call ``run()`` on it from a Python thread.
    """

    remote_command_received = Signal(str)

    def __init__(self, bot_token: Optional[str] = None, allowed_chat_id: Optional[str] = None, parent=None,
                 callback_handler: Optional[Callable[[dict], object]] = None):
        QThread.__init__(self, parent)
        TelegramRemoteListener.__init__(self, bot_token, allowed_chat_id, callback_handler)

    def run(self) -> None:
        TelegramRemoteListener.run(self)
