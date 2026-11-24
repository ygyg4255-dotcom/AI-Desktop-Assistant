"""Lightweight OpenAI-compatible chat adapter (OpenAI, Gemini, Ollama, custom). stdlib only.

Speaks the Anthropic Messages shape in and out, so the executor is unchanged: it takes an
Anthropic-style payload (system / messages / tools) and returns ``{"content": [text|tool_use
blocks], "stop_reason", "usage"}``. Anthropic itself stays on ``AIClient`` (prompt caching).
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from src.config.log import get_logger, redact_secrets

logger = get_logger(__name__)

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
FIXED_BASES = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}
_FINISH = {"tool_calls": "tool_use", "length": "max_tokens"}


def is_loopback_url(url: str) -> bool:
    p = urlparse(url or "")
    return p.scheme in ("http", "https") and p.hostname in _LOCAL_HOSTS


def validate_base_url(provider: str, url: str) -> Optional[str]:
    """Error message, or None. Ollama: loopback only. Custom: https, or loopback http."""
    if provider == "ollama" and not is_loopback_url(url):
        return "Ollama base URL must be an http(s) loopback URL (127.0.0.1, localhost or ::1)"
    if provider == "custom":
        p = urlparse(url or "")
        if not (p.hostname and (p.scheme == "https" or is_loopback_url(url))):
            return "Custom base URL must be https:// (or a loopback http:// URL)"
    return None


def chat_url(provider: str, base_url: str) -> str:
    if provider in FIXED_BASES:
        return FIXED_BASES[provider] + "/chat/completions"
    err = validate_base_url(provider, base_url)
    if err:
        raise ValueError(err)
    base = base_url.rstrip("/")
    if provider == "ollama" and not base.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions"


# ---- Anthropic -> OpenAI ---------------------------------------------------

def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _image_part(block: Dict[str, Any]) -> Dict[str, Any]:
    src = block.get("source") or {}
    return {"type": "image_url", "image_url": {"url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"}}


def to_openai_messages(system: Any, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    sys_text = _text(system)
    if sys_text:
        out.append({"role": "system", "content": sys_text})
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
            continue
        blocks = [b for b in c or [] if isinstance(b, dict)]
        if m["role"] == "assistant":
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in blocks if b.get("type") == "tool_use"]
            msg: Dict[str, Any] = {"role": "assistant", "content": _text(blocks) or None}
            if calls:
                msg["tool_calls"] = calls
            if msg["content"] or calls:
                out.append(msg)
            continue
        parts: List[Dict[str, Any]] = []
        for b in blocks:  # tool results must directly follow the assistant's tool_calls
            if b.get("type") == "tool_result":
                inner = b.get("content")
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": _text(inner)})
                parts += [_image_part(i) for i in inner or [] if isinstance(inner, list) and isinstance(i, dict) and i.get("type") == "image"]
        for b in blocks:
            if b.get("type") == "text":
                parts.append({"type": "text", "text": b.get("text", "")})
            elif b.get("type") == "image":
                parts.append(_image_part(b))
        if parts:
            out.append({"role": "user", "content": parts})
    return out


def to_openai_request(payload: Dict[str, Any], model: str, stream: bool = False) -> Dict[str, Any]:
    body: Dict[str, Any] = {"model": model, "messages": to_openai_messages(payload.get("system"), payload.get("messages") or [])}
    if payload.get("max_tokens"):
        body["max_tokens"] = payload["max_tokens"]
    tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                               "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
             for t in payload.get("tools") or []]
    if tools:
        body["tools"] = tools
    if stream:
        body["stream"] = True
    return body


# ---- OpenAI -> Anthropic ---------------------------------------------------

def _usage(u: Optional[Dict[str, Any]]) -> Dict[str, int]:
    u = u or {}
    return {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}


def _tool_input(raw: Any) -> Dict[str, Any]:
    try:
        v = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def from_openai_response(data: Dict[str, Any], model: str) -> Dict[str, Any]:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks: List[Dict[str, Any]] = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        blocks.append({"type": "tool_use", "id": tc.get("id") or f"call_{i}", "name": fn.get("name", ""),
                       "input": _tool_input(fn.get("arguments"))})
    stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else _FINISH.get(choice.get("finish_reason"), "end_turn")
    return {"content": blocks, "model": data.get("model", model), "stop_reason": stop, "usage": _usage(data.get("usage"))}


def parse_openai_stream(lines, model: str, on_text: Optional[Callable[[str], None]] = None,
                        is_cancelled: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    text = ""
    calls: List[Dict[str, Any]] = []
    finish, usage = None, None
    for raw in lines:
        if is_cancelled and is_cancelled():
            raise RuntimeError("Request cancelled")
        line = (raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw).strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if body == "[DONE]":
            break
        try:
            chunk = json.loads(body)
        except ValueError:
            continue
        if "error" in chunk:
            raise ConnectionError("Stream error from provider")
        usage = chunk.get("usage") or usage
        for ch in chunk.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                text += d["content"]
                if on_text:
                    on_text(d["content"])
            for tc in d.get("tool_calls") or []:
                idx = tc.get("index")
                if idx is None:  # Gemini sends whole calls without an index
                    # a chunk carrying a name starts a new call; otherwise it continues the last one
                    idx = len(calls) if (not calls or (tc.get("function") or {}).get("name")) else len(calls) - 1
                while len(calls) <= idx:
                    calls.append({"id": "", "name": "", "args": ""})
                fn = tc.get("function") or {}
                calls[idx]["id"] = tc.get("id") or calls[idx]["id"]
                calls[idx]["name"] += fn.get("name") or ""
                calls[idx]["args"] += fn.get("arguments") or ""
            finish = ch.get("finish_reason") or finish
    blocks: List[Dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    blocks += [{"type": "tool_use", "id": c["id"] or f"call_{i}", "name": c["name"], "input": _tool_input(c["args"])}
               for i, c in enumerate(calls)]
    stop = "tool_use" if calls else _FINISH.get(finish, "end_turn")
    return {"content": blocks, "model": model, "stop_reason": stop, "usage": _usage(usage)}


# ---- client ----------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class OpenAICompatClient:
    def __init__(self, provider: str, base_url: str, api_key: str, model: str, timeout: float = 120.0):
        self.provider, self._base_url, self._api_key = provider, base_url or "", api_key or ""
        self._model, self._timeout = model, timeout
        self._cancel_flag = False

    def cancel(self) -> None:
        self._cancel_flag = True

    def _open(self, body: Dict[str, Any], max_attempts: int, backoff: float):
        url = chat_url(self.provider, self._base_url)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        # never route loopback traffic through a system proxy
        # redirects are blocked so the Bearer key can never follow a 3xx to another host
        handlers = [_NoRedirect()] + ([urllib.request.ProxyHandler({})] if is_loopback_url(url) else [])
        opener = urllib.request.build_opener(*handlers)
        last = "Max attempts exceeded"
        for attempt in range(1, max_attempts + 1):
            if self._cancel_flag:
                raise RuntimeError("Request cancelled")
            try:
                return opener.open(urllib.request.Request(url, data=data, headers=headers, method="POST"), timeout=self._timeout)
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:160]
                except Exception:  # noqa: BLE001
                    detail = ""
                last = redact_secrets(f"HTTP {e.code}: {detail}", self._api_key)
                if e.code not in (429, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = redact_secrets(f"Connection error: {getattr(e, 'reason', e)}", self._api_key)
            if attempt < max_attempts:
                time.sleep(backoff)
        raise ConnectionError(last)

    def make_tool_request(self, payload: Dict[str, Any], max_attempts: int = 3, backoff: float = 1.5) -> Dict[str, Any]:
        self._cancel_flag = False
        with self._open(to_openai_request(payload, self._model), max_attempts, backoff) as resp:
            return from_openai_response(json.loads(resp.read().decode("utf-8")), self._model)

    def stream_tool_request(self, payload: Dict[str, Any], on_text: Optional[Callable[[str], None]] = None,
                            max_attempts: int = 3, backoff: float = 1.5) -> Dict[str, Any]:
        self._cancel_flag = False
        with self._open(to_openai_request(payload, self._model, stream=True), max_attempts, backoff) as resp:
            return parse_openai_stream(resp, self._model, on_text, lambda: self._cancel_flag)

    def test_connection(self):
        try:
            r = self.make_tool_request({"max_tokens": 10, "system": "Reply only with 'pong'.",
                                        "messages": [{"role": "user", "content": "ping"}]}, max_attempts=1)
        except Exception as e:  # noqa: BLE001 - surfaced to the HUD as a message
            return False, redact_secrets(str(e), self._api_key)
        if r["content"]:
            return True, f"Connected to {self.provider} ({self._model}) successfully!"
        return False, "Received empty response from provider."
