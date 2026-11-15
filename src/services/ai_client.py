"""Anthropic-compatible API client for OmniRoute integration."""
import json
import asyncio
import time
import socket
from typing import Optional, List, Dict, Any, Callable, Union
from dataclasses import dataclass, field
from enum import Enum
import urllib.request
import urllib.error
import threading
from concurrent.futures import Future


class ConnectionStatus(Enum):
    """Connection status enumeration."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class Message:
    """Represents a conversation message."""
    role: str  # "user" or "assistant"
    content: str
    timestamp: float = field(default_factory=lambda: 0.0)

    def to_api_format(self) -> Dict[str, Any]:
        """Convert to API format."""
        return {
            "role": self.role,
            "content": self.content
        }


@dataclass
class AIResponse:
    """Represents an AI API response."""
    content: str
    model: str
    stop_reason: str
    usage: Dict[str, int]
    raw_response: Dict[str, Any] = field(default_factory=dict)


class AIClient:
    """Anthropic-compatible API client for OmniRoute."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "serve",
        max_tokens: int = 4096,
        timeout: float = 120.0
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._status = ConnectionStatus.DISCONNECTED
        self._last_error: Optional[str] = None
        self._cancel_flag = False

    @property
    def status(self) -> ConnectionStatus:
        """Get current connection status."""
        return self._status

    @property
    def last_error(self) -> Optional[str]:
        """Get last error message."""
        return self._last_error

    @property
    def model(self) -> str:
        """Get model identifier."""
        return self._model

    def _build_request(
        self,
        messages: List[Message],
        system: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build the API request payload."""
        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [m.to_api_format() for m in messages]
        }
        if system:
            payload["system"] = system
        return payload

    def chat(
        self,
        messages: Union[List[Message], str],
        system: Optional[str] = None
    ) -> AIResponse:
        """Send a chat request synchronously with automatic retries."""
        if isinstance(messages, str):
            messages = [Message(role="user", content=messages, timestamp=time.time())]
        payload = self._build_request(messages, system)
        return self._make_request(payload)

    def _make_request(
        self,
        payload: Dict[str, Any],
        max_attempts: int = 3,
        backoff: float = 1.5
    ) -> AIResponse:
        """Make HTTP request to the API with automatic retry on gateway errors and timeouts."""
        self._cancel_flag = False

        url = f"{self._base_url}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01"
        }

        for attempt in range(1, max_attempts + 1):
            if self._cancel_flag:
                raise asyncio.CancelledError("Request cancelled")

            self._status = ConnectionStatus.CONNECTING
            try:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers=headers,
                    method="POST"
                )

                with urllib.request.urlopen(req, timeout=self._timeout) as response:
                    if self._cancel_flag:
                        raise asyncio.CancelledError("Request cancelled")

                    response_data = json.loads(response.read().decode("utf-8"))

                self._status = ConnectionStatus.CONNECTED
                self._last_error = None

                content = ""
                if "content" in response_data and response_data["content"]:
                    for block in response_data["content"]:
                        if block.get("type") == "text":
                            content += block.get("text", "")

                return AIResponse(
                    content=content,
                    model=response_data.get("model", self._model),
                    stop_reason=response_data.get("stop_reason", "end_turn"),
                    usage=response_data.get("usage", {}),
                    raw_response=response_data
                )

            except urllib.error.HTTPError as e:
                self._status = ConnectionStatus.ERROR
                error_body = ""
                try:
                    error_body = e.read().decode("utf-8")
                except Exception:
                    pass
                self._last_error = f"HTTP {e.code}: {error_body}"
                if e.code in (429, 502, 503, 504) and attempt < max_attempts and not self._cancel_flag:
                    time.sleep(backoff)
                    continue
                raise ConnectionError(self._last_error)

            except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
                self._status = ConnectionStatus.ERROR
                reason = getattr(e, "reason", str(e))
                self._last_error = f"Connection failed: {reason}"
                if attempt < max_attempts and not self._cancel_flag:
                    time.sleep(backoff)
                    continue
                raise ConnectionError(self._last_error)

            except json.JSONDecodeError as e:
                self._status = ConnectionStatus.ERROR
                self._last_error = f"Invalid response: {e}"
                raise ValueError(self._last_error)

            except Exception as e:
                self._status = ConnectionStatus.ERROR
                self._last_error = str(e)
                raise

    def send_message(
        self,
        messages: List[Message],
        system: Optional[str] = None,
        on_complete: Optional[Callable[[AIResponse], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None
    ) -> Future:
        """Send a message asynchronously and return a Future."""
        future: Future = Future()
        payload = self._build_request(messages, system)

        def _worker():
            try:
                response = self._make_request(payload)
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
        """Cancel any pending request."""
        self._cancel_flag = True

    def test_connection(self) -> bool:
        """Test connection to the API server."""
        try:
            url = f"{self._base_url}/v1/messages"
            headers = {
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01"
            }
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=5.0) as response:
                self._status = ConnectionStatus.CONNECTED
                return True
        except Exception:
            pass

        try:
            test_msg = [Message(role="user", content="ping")]
            test_payload = self._build_request(test_msg, system="Reply with 'pong' only.")
            test_payload["max_tokens"] = 10
            self._make_request(test_payload)
            return True
        except Exception:
            return False
