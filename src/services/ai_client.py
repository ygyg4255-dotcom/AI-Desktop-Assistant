"""AI client for Anthropic Claude (native Messages API only).

The endpoint is hard-coded to the official Anthropic API so a configuration
change can never redirect the API key to another host (audit S2).
"""
import json
import time
import socket
import urllib.request
import urllib.error
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any, Callable, Union, Tuple

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# Single source of truth for the default model IDs.
from src.config.settings import DEFAULT_MODEL, DEFAULT_FAST_MODEL  # noqa: E402


CACHE_CONTROL = {"type": "ephemeral"}


def apply_prompt_caching(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``payload`` with Anthropic prompt-cache breakpoints set.

    The system prompt becomes a text block and the last tool definition each get
    ``cache_control: ephemeral``, so repeated turns reuse the cached prefix
    (tools + system) and the time to first token drops. The input is not mutated.
    """
    out = dict(payload)
    system = out.get("system")
    if isinstance(system, str) and system.strip():
        out["system"] = [{"type": "text", "text": system, "cache_control": dict(CACHE_CONTROL)}]
    elif isinstance(system, list) and system and isinstance(system[-1], dict):
        blocks = [dict(b) if isinstance(b, dict) else b for b in system]
        blocks[-1].setdefault("cache_control", dict(CACHE_CONTROL))
        out["system"] = blocks
    tools = out.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        new_tools = list(tools)
        new_tools[-1] = {**tools[-1], "cache_control": dict(CACHE_CONTROL)}
        out["tools"] = new_tools
    return out


class ConnectionStatus(Enum):
    """Connection status to AI API."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class Message:
    """Represents a chat message."""
    role: str
    content: Union[str, List[Dict[str, Any]]]
    timestamp: float = field(default_factory=time.time)

    def to_api_format(self) -> Dict[str, Any]:
        """Convert to API format."""
        return {
            "role": self.role,
            "content": self.content
        }

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        """Create from dictionary."""
        return cls(
            role=data["role"],
            content=data["content"],
            timestamp=data.get("timestamp", time.time())
        )


@dataclass
class AIResponse:
    """Represents an AI API response."""
    content: str
    model: str
    stop_reason: str
    usage: Dict[str, int]
    raw_response: Dict[str, Any] = field(default_factory=dict)



def parse_sse_stream(
    lines,
    on_text: Optional[Callable[[str], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Fold a Messages API SSE stream into the same dict a non-streaming call returns.

    Text deltas are passed to ``on_text`` as they arrive; tool_use blocks are
    only assembled (their JSON input is joined and parsed at block end), so the
    caller sees complete tool calls exactly as before and the permission gate
    in the executor is unaffected. ``lines`` yields raw bytes or str lines.
    """
    message: Dict[str, Any] = {"content": [], "usage": {}}
    blocks: Dict[int, Dict[str, Any]] = {}
    json_parts: Dict[int, List[str]] = {}
    event = ""
    for raw in lines:
        if is_cancelled and is_cancelled():
            raise RuntimeError("Request cancelled")
        line = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.rstrip("\r\n")
        if not line:
            event = ""
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[5:].strip())
        except ValueError:
            continue
        kind = data.get("type") or event
        if kind == "message_start":
            msg = data.get("message", {})
            for key in ("id", "model", "role", "usage"):
                if key in msg:
                    message[key] = msg[key]
        elif kind == "content_block_start":
            idx = data.get("index", len(blocks))
            block = dict(data.get("content_block", {}))
            if block.get("type") == "tool_use":
                json_parts[idx] = []
            blocks[idx] = block
        elif kind == "content_block_delta":
            idx = data.get("index", 0)
            block = blocks.setdefault(idx, {"type": "text", "text": ""})
            delta = data.get("delta", {})
            dtype = delta.get("type")
            if dtype == "text_delta":
                text = delta.get("text", "")
                block["text"] = block.get("text", "") + text
                if text and on_text:
                    on_text(text)
            elif dtype == "input_json_delta":
                json_parts.setdefault(idx, []).append(delta.get("partial_json", ""))
            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif dtype == "signature_delta":
                block["signature"] = delta.get("signature", "")
        elif kind == "content_block_stop":
            idx = data.get("index", 0)
            block = blocks.get(idx)
            if block is not None and block.get("type") == "tool_use":
                raw_json = "".join(json_parts.pop(idx, [])).strip()
                try:
                    block["input"] = json.loads(raw_json) if raw_json else {}
                except ValueError:
                    block["input"] = {}
        elif kind == "message_delta":
            delta = data.get("delta", {})
            if "stop_reason" in delta:
                message["stop_reason"] = delta["stop_reason"]
            message["usage"] = {**message.get("usage", {}), **data.get("usage", {})}
        elif kind == "error":
            err = data.get("error", {})
            raise ConnectionError(f"Stream error: {err.get('type', 'error')}: {str(err.get('message', ''))[:160]}")
    message["content"] = [blocks[i] for i in sorted(blocks)]
    message.setdefault("stop_reason", "end_turn")
    return message


class AIClient:
    """Anthropic Claude client. The endpoint is fixed; only key and model are configurable."""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        provider: str = "anthropic",
        max_tokens: int = 4096,
        timeout: float = 120.0
    ):
        # base_url and provider are accepted for backward compatibility with
        # existing callers but are ignored: requests always go to Anthropic.
        self._api_key = api_key or ""
        self._model = self._normalize_model(model)
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._status = ConnectionStatus.DISCONNECTED
        self._last_error: Optional[str] = None
        self._cancel_flag = False

    @staticmethod
    def _normalize_model(model: Optional[str]) -> str:
        m = (model or "").strip()
        return DEFAULT_MODEL if m in ("", "serve") else m

    def update_config(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None
    ) -> None:
        """Update model / API key at runtime. provider and base_url are ignored."""
        if model is not None:
            self._model = self._normalize_model(model)
        if api_key is not None:
            self._api_key = api_key.strip()

    @property
    def provider(self) -> str:
        return "anthropic"

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return ANTHROPIC_MESSAGES_URL

    def _get_endpoint_url(self) -> str:
        return ANTHROPIC_MESSAGES_URL

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    def make_tool_request(
        self,
        payload: Dict[str, Any],
        max_attempts: int = 3,
        backoff: float = 1.5
    ) -> Dict[str, Any]:
        """Execute a (tool-enabled) Messages API request."""
        self._cancel_flag = False
        req_payload = apply_prompt_caching(payload)
        req_payload["model"] = self._model
        return self._dispatch_http_json(
            self._get_endpoint_url(), self._get_headers(), req_payload, max_attempts, backoff
        )

    def stream_tool_request(
        self,
        payload: Dict[str, Any],
        on_text: Optional[Callable[[str], None]] = None,
        max_attempts: int = 3,
        backoff: float = 1.5,
    ) -> Dict[str, Any]:
        """Like ``make_tool_request`` but streams (``stream=True``), calling ``on_text`` per text delta.

        Returns the same response dict shape. Retries only happen before the first
        byte of the stream arrives; once text has been delivered a failure is raised
        rather than replayed, so nothing is spoken twice.
        """
        self._cancel_flag = False
        req_payload = apply_prompt_caching(payload)
        req_payload["model"] = self._model
        req_payload["stream"] = True
        headers = dict(self._get_headers())
        headers["Accept"] = "text/event-stream"
        data = json.dumps(req_payload, ensure_ascii=False).encode("utf-8")

        for attempt in range(1, max_attempts + 1):
            if self._cancel_flag:
                raise RuntimeError("Request cancelled")
            self._status = ConnectionStatus.CONNECTING
            started = {"n": 0}

            def _counted(text: str) -> None:
                started["n"] += 1
                if on_text:
                    on_text(text)

            try:
                req = urllib.request.Request(self._get_endpoint_url(), data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    parsed = parse_sse_stream(resp, _counted, lambda: self._cancel_flag)
                self._status = ConnectionStatus.CONNECTED
                self._last_error = None
                return parsed
            except urllib.error.HTTPError as e:
                self._status = ConnectionStatus.ERROR
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8")
                except Exception:
                    pass
                self._last_error = f"HTTP {e.code}: {err_text[:160]}"
                if e.code in (429, 502, 503, 504) and attempt < max_attempts and not self._cancel_flag:
                    time.sleep(backoff)
                    continue
                raise ConnectionError(self._last_error)
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as e:
                self._status = ConnectionStatus.ERROR
                reason = getattr(e, "reason", str(e))
                self._last_error = f"Connection error: {reason}"
                if started["n"] == 0 and attempt < max_attempts and not self._cancel_flag:
                    time.sleep(backoff)
                    continue
                raise ConnectionError(self._last_error)
            except Exception as e:
                self._status = ConnectionStatus.ERROR
                self._last_error = str(e)
                raise

        raise ConnectionError(self._last_error or "Max attempts exceeded")

    def _dispatch_http_json(
        self,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        max_attempts: int,
        backoff: float
    ) -> Dict[str, Any]:
        """Make raw HTTP POST and parse JSON response with exponential retry."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        for attempt in range(1, max_attempts + 1):
            if self._cancel_flag:
                raise RuntimeError("Request cancelled")

            self._status = ConnectionStatus.CONNECTING
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    if self._cancel_flag:
                        raise RuntimeError("Request cancelled")
                    parsed = json.loads(resp.read().decode("utf-8"))
                    self._status = ConnectionStatus.CONNECTED
                    self._last_error = None
                    return parsed

            except urllib.error.HTTPError as e:
                self._status = ConnectionStatus.ERROR
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8")
                except Exception:
                    pass

                self._last_error = f"HTTP {e.code}: {err_text[:160]}"

                if e.code in (429, 502, 503, 504) and attempt < max_attempts and not self._cancel_flag:
                    time.sleep(backoff)
                    continue
                raise ConnectionError(self._last_error)

            except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
                self._status = ConnectionStatus.ERROR
                reason = getattr(e, "reason", str(e))
                self._last_error = f"Connection error: {reason}"
                if attempt < max_attempts and not self._cancel_flag:
                    time.sleep(backoff)
                    continue
                raise ConnectionError(self._last_error)

            except Exception as e:
                self._status = ConnectionStatus.ERROR
                self._last_error = str(e)
                raise

        raise ConnectionError(self._last_error or "Max attempts exceeded")

    def chat(
        self,
        messages: Union[List[Message], str],
        system: Optional[str] = None
    ) -> AIResponse:
        """Send a chat request synchronously."""
        if isinstance(messages, str):
            messages = [Message(role="user", content=messages, timestamp=time.time())]

        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [m.to_api_format() for m in messages]
        }
        if system:
            payload["system"] = system

        raw_resp = self.make_tool_request(payload)
        content_str = ""
        for block in raw_resp.get("content", []):
            if block.get("type") == "text":
                content_str += block.get("text", "")

        return AIResponse(
            content=content_str,
            model=raw_resp.get("model", self._model),
            stop_reason=raw_resp.get("stop_reason", "end_turn"),
            usage=raw_resp.get("usage", {}),
            raw_response=raw_resp
        )

    def send_message(
        self,
        messages: List[Message],
        system: Optional[str] = None,
        on_complete: Optional[Callable[[AIResponse], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None
    ) -> Future:
        """Send a message asynchronously and return a Future."""
        future: Future = Future()

        def _worker():
            try:
                response = self.chat(messages, system)
                future.set_result(response)
                if on_complete:
                    on_complete(response)
            except Exception as e:
                future.set_exception(e)
                if on_error:
                    on_error(e)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        return future

    def cancel(self) -> None:
        """Cancel pending request."""
        self._cancel_flag = True

    def test_connection(self) -> Tuple[bool, str]:
        """Verify API connectivity with a fast single-attempt test utterance, returning (success, message)."""
        try:
            payload = {
                "model": self._model,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
                "system": "Reply only with 'pong'."
            }
            raw_resp = self.make_tool_request(payload, max_attempts=1)
            content_str = ""
            for block in raw_resp.get("content", []):
                if block.get("type") == "text":
                    content_str += block.get("text", "")
            if content_str or raw_resp.get("content"):
                return True, f"Connected to Anthropic ({self._model}) successfully!"
            return False, "Received empty response from provider."
        except Exception as e:
            return False, str(e)
