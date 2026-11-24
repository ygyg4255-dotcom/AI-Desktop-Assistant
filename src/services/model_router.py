"""Provider router: the active AI provider (AI_PROVIDER) serves every request.

``anthropic`` (default) delegates to ``AIClient`` untouched, so prompt caching is unchanged.
``openai`` / ``gemini`` / ``ollama`` / ``custom`` go through the stdlib OpenAI-compatible
adapter. ``configure()`` re-reads .env, so a HUD save switches provider live. Every other
attribute is delegated to the Anthropic client, so the executor needs no changes.
"""

from typing import Callable, Optional

from src.config.log import get_logger
from src.config.settings import PROVIDER_ENV, get_active_provider, get_config_value, get_provider_config
from src.services.openai_compat import OpenAICompatClient, is_loopback_url  # noqa: F401 - re-export

logger = get_logger(__name__)


class ModelRouter:
    def __init__(self, anthropic_client, provider: Optional[str] = None):
        self._anthropic = anthropic_client
        self.configure(provider)

    def configure(self, provider: Optional[str] = None) -> None:
        """(Re)select the provider live; None reads AI_PROVIDER from .env/environment."""
        provider = provider if provider in PROVIDER_ENV else get_active_provider()
        self.provider = provider
        self._compat = None
        if provider != "anthropic":
            c = get_provider_config(provider)
            kw = {}
            if provider == "ollama":  # local models should fail fast; OLLAMA_TIMEOUT overrides
                try:
                    kw["timeout"] = float(get_config_value("OLLAMA_TIMEOUT", default="30"))
                except (TypeError, ValueError):
                    kw["timeout"] = 30.0
            self._compat = OpenAICompatClient(provider, c["base_url"], c["api_key"], c["model"], **kw)
        logger.info("AI provider: %s", provider)

    def __getattr__(self, name):  # only called for attributes not defined here
        return getattr(self._anthropic, name)

    def _client(self):
        return self._compat or self._anthropic

    def make_tool_request(self, payload, max_attempts: int = 3, backoff: float = 1.5):
        return self._client().make_tool_request(payload, max_attempts=max_attempts, backoff=backoff)

    def stream_tool_request(self, payload, on_text: Optional[Callable[[str], None]] = None,
                            max_attempts: int = 3, backoff: float = 1.5):
        return self._client().stream_tool_request(payload, on_text=on_text, max_attempts=max_attempts, backoff=backoff)

    def cancel(self) -> None:
        self._anthropic.cancel()
        if self._compat:
            self._compat.cancel()
