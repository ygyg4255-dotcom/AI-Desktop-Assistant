"""Universal AI Client supporting multiple LLM providers:
- Anthropic Claude (native Messages API)
- Ollama (local OpenAI-compatible /v1/chat/completions)
- OpenAI (official Chat Completions API)
- Google Gemini (OpenAI-compatible /v1beta/openai/chat/completions + native generateContent fallback)
- Custom OpenAI-Compatible (Groq, DeepSeek, OpenRouter, LM Studio, OmniRoute)
"""
import os
import sys
import json
import time
import socket
import asyncio
import urllib.request
import urllib.error
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any, Callable, Union, Tuple


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


def _clean_schema_for_gemini(val: Any) -> Any:
    """Strip schema fields not supported by Gemini's endpoint (additionalProperties, strict, $schema)."""
    if isinstance(val, dict):
        cleaned = {}
        for k, v in val.items():
            if k in ("additionalProperties", "strict", "$schema"):
                continue
            cleaned[k] = _clean_schema_for_gemini(v)
        return cleaned
    elif isinstance(val, list):
        return [_clean_schema_for_gemini(x) for x in val]
    return val


class AIClient:
    """Universal Multi-Provider AI Client for Anthropic, Ollama, OpenAI, Gemini, and Custom APIs."""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "serve",
        provider: str = "anthropic",
        max_tokens: int = 4096,
        timeout: float = 120.0
    ):
        self._provider = (provider or "anthropic").lower().strip()
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._model = model or "serve"
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._status = ConnectionStatus.DISCONNECTED
        self._last_error: Optional[str] = None
        self._cancel_flag = False
        self._normalize_config()

    def update_config(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None
    ) -> None:
        """Dynamically update client configuration at runtime."""
        if provider is not None:
            self._provider = provider.lower().strip()
        if model is not None:
            self._model = model.strip()
        if api_key is not None:
            self._api_key = api_key.strip()
        if base_url is not None:
            self._base_url = base_url.rstrip("/")
        self._normalize_config()

    def _normalize_config(self) -> None:
        """Apply smart defaults per provider when fields are blank."""
        p = self._provider
        if p == "ollama":
            if not self._base_url:
                self._base_url = "http://localhost:11434"
            if not self._model or self._model == "serve":
                self._model = "llama3.2"
        elif p == "openai":
            if not self._base_url:
                self._base_url = "https://api.openai.com/v1"
            if not self._model or self._model == "serve":
                self._model = "gpt-4o-mini"
        elif p == "gemini":
            if not self._base_url:
                self._base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
            if not self._model or self._model in ("serve", "gemini-1.5-flash", "gemini-3.0-flash"):
                self._model = "gemini-2.5-flash"
        elif p == "anthropic":
            if not self._base_url:
                self._base_url = "https://api.anthropic.com"
            if not self._model or self._model == "serve":
                self._model = "claude-3-5-sonnet-20241022"
        elif p == "custom":
            if not self._base_url:
                self._base_url = "http://localhost:20128"

    @property
    def provider(self) -> str:
        return self._provider

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
        return self._base_url

    def _is_anthropic_wire(self) -> bool:
        """Check if target provider communicates via native Anthropic messages protocol."""
        return self._provider == "anthropic" or (
            self._provider == "custom" and "/v1/messages" in self._base_url
        )

    def _get_endpoint_url(self) -> str:
        """Compute the full HTTP POST endpoint URL based on the active provider."""
        if self._is_anthropic_wire():
            if self._base_url.endswith("/v1/messages"):
                return self._base_url
            if self._base_url.endswith("/v1"):
                return f"{self._base_url}/messages"
            return f"{self._base_url}/v1/messages"

        # OpenAI-compatible chat completions
        if self._base_url.endswith("/chat/completions"):
            return self._base_url
        if self._base_url.endswith("/v1") or self._base_url.endswith("/openai"):
            return f"{self._base_url}/chat/completions"
        return f"{self._base_url}/v1/chat/completions"

    def _get_headers(self) -> Dict[str, str]:
        """Generate appropriate HTTP request headers."""
        headers = {"Content-Type": "application/json"}
        if self._is_anthropic_wire():
            headers["x-api-key"] = self._api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
                if self._provider == "gemini":
                    headers["x-goog-api-key"] = self._api_key
        return headers

    def make_tool_request(
        self,
        payload: Dict[str, Any],
        max_attempts: int = 3,
        backoff: float = 1.5
    ) -> Dict[str, Any]:
        """Execute a tool-enabled request against the active AI provider with translation."""
        self._cancel_flag = False
        url = self._get_endpoint_url()
        headers = self._get_headers()

        # If native Anthropic wire format, dispatch directly
        if self._is_anthropic_wire():
            req_payload = dict(payload)
            req_payload["model"] = self._model
            return self._dispatch_http_json(url, headers, req_payload, max_attempts, backoff)

        # Translate Anthropic payload -> OpenAI format
        openai_messages: List[Dict[str, Any]] = []
        if payload.get("system"):
            openai_messages.append({"role": "system", "content": payload["system"]})

        for m in payload.get("messages", []):
            role = m.get("role", "user")
            content = m.get("content", "")

            if isinstance(content, str):
                openai_messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                text_parts = []
                tool_calls = []
                for b in content:
                    b_type = b.get("type")
                    if b_type == "text":
                        text_parts.append(b.get("text", ""))
                    elif b_type == "tool_use":
                        tool_calls.append({
                            "id": b.get("id"),
                            "type": "function",
                            "function": {
                                "name": b.get("name"),
                                "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)
                            }
                        })
                    elif b_type == "tool_result":
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": b.get("tool_use_id"),
                            "content": str(b.get("content", ""))
                        })

                if text_parts or tool_calls:
                    msg_obj: Dict[str, Any] = {"role": role}
                    if text_parts:
                        msg_obj["content"] = " ".join(text_parts)
                    if tool_calls:
                        msg_obj["tool_calls"] = tool_calls
                    openai_messages.append(msg_obj)

        openai_tools: List[Dict[str, Any]] = []
        for t in payload.get("tools", []):
            schema = t.get("input_schema", {"type": "object", "properties": {}})
            if self._provider == "gemini":
                schema = _clean_schema_for_gemini(schema)
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": schema
                }
            })

        req_payload = {
            "model": self._model,
            "messages": openai_messages,
            "max_tokens": payload.get("max_tokens", self._max_tokens)
        }
        if openai_tools:
            req_payload["tools"] = openai_tools

        try:
            raw_resp = self._dispatch_http_json(url, headers, req_payload, max_attempts, backoff)
        except Exception as api_err:
            # If provider is Gemini and OpenAI-compatible endpoint failed, fall back to native REST
            if self._provider == "gemini" and self._api_key:
                try:
                    return self._make_gemini_native_request(payload, max_attempts=2, backoff=backoff)
                except Exception as native_err:
                    raise ConnectionError(f"Gemini API error (REST fallback also failed: {native_err}) from {api_err}")
            raise

        # Translate OpenAI format response -> Normalized Anthropic dictionary
        choice = raw_resp.get("choices", [{}])[0].get("message", {})
        text_content = choice.get("content") or ""
        tool_calls = choice.get("tool_calls") or []

        content_blocks: List[Dict[str, Any]] = []
        if text_content:
            content_blocks.append({"type": "text", "text": text_content})

        for tc in tool_calls:
            fn = tc.get("function", {})
            args_raw = fn.get("arguments", "{}")
            try:
                parsed_args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except Exception:
                parsed_args = {}

            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id", f"call_{int(time.time() * 1000)}"),
                "name": fn.get("name"),
                "input": parsed_args
            })

        return {
            "id": raw_resp.get("id", "msg_unified"),
            "model": raw_resp.get("model", self._model),
            "content": content_blocks,
            "stop_reason": "tool_use" if tool_calls else "end_turn",
            "usage": raw_resp.get("usage", {})
        }

    def _make_gemini_native_request(
        self,
        payload: Dict[str, Any],
        max_attempts: int = 2,
        backoff: float = 1.5
    ) -> Dict[str, Any]:
        """Native Google Gemini REST fallback (models.generateContent)."""
        model_name = self._model.replace("models/", "")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self._api_key}"
        headers = {"Content-Type": "application/json"}

        contents = []
        for m in payload.get("messages", []):
            role = "user" if m.get("role") in ("user", "system") else "model"
            content = m.get("content", "")
            parts = []
            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for b in content:
                    if b.get("type") == "text":
                        parts.append({"text": b.get("text", "")})
                    elif b.get("type") == "tool_use":
                        parts.append({"functionCall": {"name": b.get("name"), "args": b.get("input", {})}})
                    elif b.get("type") == "tool_result":
                        parts.append({
                            "functionResponse": {
                                "name": b.get("tool_name", "tool"),
                                "response": {"result": str(b.get("content", ""))}
                            }
                        })
            if parts:
                contents.append({"role": role, "parts": parts})

        native_payload: Dict[str, Any] = {"contents": contents}
        if payload.get("system"):
            native_payload["systemInstruction"] = {"parts": [{"text": payload["system"]}]}

        native_tools = []
        for t in payload.get("tools", []):
            schema = _clean_schema_for_gemini(t.get("input_schema", {"type": "object", "properties": {}}))
            native_tools.append({
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": schema
            })
        if native_tools:
            native_payload["tools"] = [{"functionDeclarations": native_tools}]

        raw_resp = self._dispatch_http_json(url, headers, native_payload, max_attempts, backoff)
        candidates = raw_resp.get("candidates", [{}])
        first_cand = candidates[0] if candidates else {}
        cand_content = first_cand.get("content", {})
        cand_parts = cand_content.get("parts", [])

        content_blocks = []
        has_tool_call = False
        for p in cand_parts:
            if "text" in p:
                content_blocks.append({"type": "text", "text": p["text"]})
            elif "functionCall" in p:
                fc = p["functionCall"]
                has_tool_call = True
                content_blocks.append({
                    "type": "tool_use",
                    "id": f"call_{int(time.time() * 1000)}",
                    "name": fc.get("name"),
                    "input": fc.get("args", {})
                })

        return {
            "id": f"gemini_{int(time.time())}",
            "model": self._model,
            "content": content_blocks,
            "stop_reason": "tool_use" if has_tool_call else "end_turn",
            "usage": raw_resp.get("usageMetadata", {})
        }

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

                # Provide friendly diagnostic for Gemini and other common errors
                if self._provider == "gemini":
                    if e.code == 400:
                        self._last_error = f"Gemini Error 400: Invalid request or API key ({err_text[:160]})"
                    elif e.code == 403:
                        self._last_error = "Gemini Error 403: API key is invalid or Generative Language API is disabled."
                    elif e.code == 429:
                        self._last_error = "Gemini Error 429: Quota limit reached. Please wait or check your AI Studio quota."
                    else:
                        self._last_error = f"Gemini Error {e.code}: {err_text[:160]}"
                else:
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
                return True, f"Connected to {self._provider.title()} ({self._model}) successfully!"
            return False, "Received empty response from provider."
        except Exception as e:
            return False, str(e)
