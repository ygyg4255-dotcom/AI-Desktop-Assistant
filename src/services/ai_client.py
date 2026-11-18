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
        req_payload = dict(payload)
        req_payload["model"] = self._model
        return self._dispatch_http_json(
            self._get_endpoint_url(), self._get_headers(), req_payload, max_attempts, backoff
        )

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
