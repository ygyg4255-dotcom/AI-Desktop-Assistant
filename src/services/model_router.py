"""Provider router: Ollama (local, simple chat only) -> Anthropic (everything else).

Wraps ``AIClient`` and exposes the same ``make_tool_request`` / ``stream_tool_request``
interface; every other attribute is delegated to the Anthropic client, so the executor
and ``apply_ai_config`` need no changes. With ``OLLAMA_ENABLED`` false (default) each call
goes straight to Anthropic untouched, so prompt caching is unchanged.
"""

import json
import re
import urllib.request
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from src.config.log import get_logger
from src.config.settings import get_config_value

logger = get_logger(__name__)

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}  # S2: Ollama is loopback only
# shortcut: keyword heuristic for "needs tools"; upgrade to a classifier if Ollama misroutes.
_ACTION_RE = re.compile(
    r"\b(open|close|launch|start|stop|run|click|type|press|delete|remove|restore|create|make|write|"
    r"save|send|search|find|list|play|pause|mute|volume|shutdown|restart|screenshot|screen|remind|"
    r"remember|forget|download|install|set|turn|move|copy|rename|read|check|look)\b"
    r"|\b(and then|then|after that)\b|[\\/]|\.\w{2,4}\b",
    re.I,
)
_MAX_SIMPLE_CHARS = 400


def is_loopback_url(url: str) -> bool:
    p = urlparse(url)
    return p.scheme in ("http", "https") and p.hostname in _LOCAL_HOSTS


def _flatten(content: Any) -> str:
    """Anthropic content (str or blocks) -> plain text; drops cache_control and non-text blocks."""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _is_simple_chat(payload: Dict[str, Any]) -> bool:
    msgs: List[Dict[str, Any]] = payload.get("messages") or []
    if not msgs or msgs[-1].get("role") != "user":
        return False
    for m in msgs:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") != "text" for b in c):
            return False  # image / tool_use / tool_result anywhere -> Anthropic
    text = _flatten(msgs[-1].get("content")).strip()
    return bool(text) and len(text) <= _MAX_SIMPLE_CHARS and not _ACTION_RE.search(text)


class ModelRouter:
    def __init__(self, anthropic_client, enabled: Optional[bool] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None, timeout: Optional[float] = None):
        self._anthropic = anthropic_client
        try:
            self._o_timeout = float(timeout if timeout is not None else get_config_value("OLLAMA_TIMEOUT", default="15"))
        except ValueError:
            self._o_timeout = 15.0
        self.configure(enabled, base_url, model)

    def configure(self, enabled: Optional[bool] = None, base_url: Optional[str] = None,
                  model: Optional[str] = None) -> None:
        """(Re)apply Ollama settings live; None falls back to .env/default."""
        if enabled is None:
            enabled = get_config_value("OLLAMA_ENABLED", default="false").strip().lower() in ("1", "true", "yes", "on")
        self._o_url = (base_url or get_config_value("OLLAMA_BASE_URL", default="http://127.0.0.1:11434")).rstrip("/")
        self._o_model = model or get_config_value("OLLAMA_MODEL", default="qwen2.5-coder:3b")
        if enabled and not is_loopback_url(self._o_url):
            logger.warning("OLLAMA_BASE_URL is not loopback; Ollama disabled")
            enabled = False
        self.enabled = enabled

    def __getattr__(self, name):  # only called for attributes not defined here
        return getattr(self._anthropic, name)

    # -- public interface (same as AIClient) --
    def make_tool_request(self, payload, max_attempts: int = 3, backoff: float = 1.5):
        resp = self._try_ollama(payload, None)
        if resp is not None:
            return resp
        return self._anthropic_call("make_tool_request", payload, max_attempts=max_attempts, backoff=backoff)

    def stream_tool_request(self, payload, on_text: Optional[Callable[[str], None]] = None,
                            max_attempts: int = 3, backoff: float = 1.5):
        resp = self._try_ollama(payload, on_text)
        if resp is not None:
            return resp
        return self._anthropic_call("stream_tool_request", payload, on_text=on_text,
                                    max_attempts=max_attempts, backoff=backoff)

    # -- internals --
    def _anthropic_call(self, method: str, payload, **kw):
        logger.info("provider=anthropic")
        return getattr(self._anthropic, method)(payload, **kw)

    def _try_ollama(self, payload, on_text) -> Optional[Dict[str, Any]]:
        if not self.enabled or not _is_simple_chat(payload):
            return None
        emitted = {"n": 0}

        def _on(t: str) -> None:
            emitted["n"] += 1
            if on_text:
                on_text(t)

        try:
            resp = self._ollama_chat(payload, _on)
            logger.info("provider=ollama model=%s", self._o_model)
            return resp
        except Exception as e:  # noqa: BLE001 - any Ollama failure must fall back, never crash
            if emitted["n"]:
                # Text already reached the user/TTS; replaying on Anthropic would double-speak.
                logger.warning("Ollama failed mid-stream (%s); keeping partial reply", type(e).__name__)
                return {"content": [{"type": "text", "text": ""}], "stop_reason": "end_turn", "usage": {}}
            logger.warning("Ollama unavailable (%s); falling back to Anthropic", type(e).__name__)
            return None

    def _ollama_chat(self, payload, on_text) -> Dict[str, Any]:
        system = _flatten(payload.get("system"))
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": m["role"], "content": _flatten(m.get("content"))} for m in payload["messages"]
        ]
        body = json.dumps({"model": self._o_model, "messages": msgs, "stream": True}).encode("utf-8")
        req = urllib.request.Request(f"{self._o_url}/api/chat", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route loopback via a proxy
        text = ""
        done = False
        with opener.open(req, timeout=self._o_timeout) as resp:
            for raw in resp:
                if not raw.strip():
                    continue
                chunk = json.loads(raw)
                if "error" in chunk:
                    raise ValueError("ollama error")
                piece = (chunk.get("message") or {}).get("content")
                if piece:
                    text += piece
                    on_text(piece)
                if chunk.get("done"):
                    done = True
                    break
        if not done or not text.strip():
            raise ValueError("malformed ollama response")
        return {"content": [{"type": "text", "text": text}], "model": self._o_model,
                "stop_reason": "end_turn", "usage": {}}
