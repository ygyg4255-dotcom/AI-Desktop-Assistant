"""Application settings and configuration (Anthropic Claude only).

Config precedence (the only order used anywhere in Altron; see get_config_value):
    1. the .env file at resolve_env_path()
    2. the process environment
    3. the built-in default
Explicit function arguments (e.g. a token passed to send_telegram_message) always
win over all three.
"""
import os
import sys
import json
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from dotenv import load_dotenv

from src.config.log import get_logger

logger = get_logger(__name__)

load_dotenv(override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# The API key is only ever sent here. Deliberately not configurable (audit S2).
ANTHROPIC_API_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_FAST_MODEL = "claude-haiku-5-5"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DATA_DIR = Path.home() / ".ai-desktop-assistant"


# Cached active config for the HUD /status poll (invalidated by save_ai_config or .env mtime).
_CONFIG_LOCK = threading.Lock()
_CONFIG_VERSION = 0
_CONFIG_CACHE: Dict[str, Any] = {"stamp": None, "env_path": None, "value": None}


def _env_stamp(env_path: Optional[Path]) -> Any:
    try:
        st = env_path.stat() if env_path else None
        return (st.st_mtime_ns, st.st_size) if st else None
    except OSError:
        return None


def invalidate_config_cache() -> None:
    global _CONFIG_VERSION
    with _CONFIG_LOCK:
        _CONFIG_VERSION += 1
    with _ENV_LOCK:
        _ENV_CACHE.update(path=None, stamp=None, vars={})


def resolve_env_path(data_dir: Optional[Path] = None) -> Path:
    """The single .env location shared by every reader and writer.

    Source checkout: <project root>/.env. Frozen build: next to the exe, or
    <data dir>/.env when only that one exists.
    """
    if getattr(sys, "frozen", False):
        exe_env = Path(sys.executable).parent / ".env"
        data_env = (data_dir or _DEFAULT_DATA_DIR) / ".env"
        if not exe_env.exists() and data_env.exists():
            return data_env
        return exe_env
    return _PROJECT_ROOT / ".env"


# Parsed .env cache keyed on (path, mtime, size) so hot paths (Telegram sends,
# HUD polls) do not re-read the file on every call.
_ENV_LOCK = threading.Lock()
_ENV_CACHE: Dict[str, Any] = {"path": None, "stamp": None, "vars": {}}


def read_env_vars(env_path: Optional[Path] = None) -> Dict[str, str]:
    """Parsed key/values of the .env file (empty dict if it is missing or unreadable)."""
    path = Path(env_path) if env_path is not None else resolve_env_path()
    stamp = _env_stamp(path)
    with _ENV_LOCK:
        if stamp is not None and _ENV_CACHE["path"] == path and _ENV_CACHE["stamp"] == stamp:
            return dict(_ENV_CACHE["vars"])
    try:
        values = load_env_file(path)
    except (OSError, UnicodeDecodeError) as err:
        logger.warning("Could not read %s, ignoring it: %s", path.name, type(err).__name__)
        values = {}
    with _ENV_LOCK:
        _ENV_CACHE.update(path=path, stamp=stamp, vars=dict(values))
    return values


def get_config_value(*keys: str, default: str = "", env_path: Optional[Path] = None) -> str:
    """Canonical config lookup: .env file, then process environment, then default.

    Within each source the keys are tried in order (aliases such as
    "AI_API_KEY", "ANTHROPIC_API_KEY") and the first non-empty value wins.
    """
    file_vars = read_env_vars(env_path)
    for source in (file_vars, os.environ):
        for key in keys:
            value = source.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return default


def get_active_config_dict() -> Dict[str, Any]:
    """Masked active config; re-reads .env only when it changed or was just saved."""
    with _CONFIG_LOCK:
        c = _CONFIG_CACHE
        if (c["value"] is not None
                and c["stamp"] == (_CONFIG_VERSION, _env_stamp(c["env_path"]))):
            return dict(c["value"])
        version = _CONFIG_VERSION
        s = Settings()
        value = s.get_config_dict()
        c.update(stamp=(version, _env_stamp(s.env_path)), env_path=s.env_path, value=value)
        return dict(value)


def _unquote_env_value(value: str) -> str:
    """Strip whitespace and one pair of matching surrounding quotes from a .env value."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _format_env_value(value: str) -> str:
    """Quote a value for .env when it would not round-trip unquoted."""
    value = str(value)
    if value and (value != value.strip() or any(c in value for c in " #\"'")):
        # load_env_file only strips one matching pair (no escapes), so pick a quote
        # character the value does not contain.
        if '"' not in value:
            return f'"{value}"'
        if "'" not in value:
            return f"'{value}'"
    return value


def load_env_file(env_path: Path) -> dict:
    """Load environment variables from .env file."""
    env_vars = {}
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = _unquote_env_value(value)
    return env_vars


def is_masked_key(key: str) -> bool:
    """Check if API key is a masked placeholder and should not overwrite existing secret."""
    if not key or not str(key).strip():
        return True
    s = str(key).strip()
    return "•" in s or "*" in s or s == "••••••••" or (len(s) > 4 and "••" in s)


def save_ai_config(
    model: str,
    api_key: str,
    language: str = "English",
    env_path: Optional[Path] = None
) -> Dict[str, str]:
    """Persist Claude model / API key / language to .env and the process environment."""
    for value in (model, api_key, language):
        if "\r" in str(value) or "\n" in str(value):
            raise ValueError("config values must not contain line breaks")

    if env_path is None:
        env_path = resolve_env_path()

    model = model.strip() or DEFAULT_MODEL
    updates = {
        "AI_MODEL": model,
        "ALTRON_LANGUAGE": language.strip(),
        "ANTHROPIC_MODEL": model,
    }
    if not is_masked_key(api_key):
        updates["AI_API_KEY"] = api_key.strip()
        updates["ANTHROPIC_API_KEY"] = api_key.strip()

    lines = []
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as err:
            # Rebuilding from scratch would drop unrelated settings, so refuse to overwrite.
            logger.error("Could not read %s: %s", env_path.name, type(err).__name__)
            raise

    keys_written = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                keys_written.add(k)
                continue
        new_lines.append(line)

    for k, v in updates.items():
        if k not in keys_written:
            new_lines.append(f"{k}={v}\n")

    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as err:
        # Never include values (they may hold the API key) in the message.
        logger.error("Failed to write %s: %s", env_path.name, type(err).__name__)
        raise

    for k, v in updates.items():
        os.environ[k] = str(v)
    invalidate_config_cache()

    return updates


@dataclass
class Settings:
    """Application settings loaded from configuration."""

    app_name: str = "Altron"
    app_version: str = "0.2.0"
    app_author: str = "Altron"

    # Paths
    data_dir: Path = field(default_factory=lambda: Path.home() / ".ai-desktop-assistant")
    db_path: Path = field(init=False)
    config_path: Path = field(init=False)
    env_path: Path = field(init=False)

    # Window settings
    window_width: int = 600
    window_height: int = 700

    # Claude settings (endpoint is fixed: see ANTHROPIC_API_URL)
    ai_model: str = DEFAULT_MODEL
    ai_fast_model: str = DEFAULT_FAST_MODEL
    ai_api_key: str = ""
    altron_language: str = "English"

    # Legacy aliases
    anthropic_api_key: str = ""
    anthropic_model: str = DEFAULT_MODEL
    max_tokens: int = 4096
    request_timeout: float = 120.0

    # Telegram remote credentials
    telegram_bot_token: str = TELEGRAM_BOT_TOKEN
    telegram_chat_id: str = TELEGRAM_CHAT_ID

    # Optional Groq key, used ONLY for Whisper speech-to-text (never for chat).
    groq_api_key: str = ""

    # Startup settings
    start_with_windows: bool = False
    show_window_on_start: bool = False

    # Voice & UI behavior settings
    enable_tts: bool = True
    dynamic_notch: bool = True

    def __post_init__(self):
        """Initialize derived paths and load environment."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "assistant.db"
        self.config_path = self.data_dir / "config.json"

        self.env_path = resolve_env_path(self.data_dir)
        self.load_environment()

    def load_environment(self):
        """Resolve every setting through get_config_value (.env > environment > default)."""
        def cfg(*keys: str, default: str = "") -> str:
            return get_config_value(*keys, default=default, env_path=self.env_path)

        model = cfg("AI_MODEL", "ANTHROPIC_MODEL", default=DEFAULT_MODEL)
        # "serve" was the old OmniRoute proxy placeholder, not a Claude model.
        self.ai_model = DEFAULT_MODEL if model == "serve" else model
        self.ai_fast_model = cfg("AI_FAST_MODEL", default=DEFAULT_FAST_MODEL)
        self.ai_api_key = cfg("AI_API_KEY", "ANTHROPIC_API_KEY")
        self.altron_language = cfg("ALTRON_LANGUAGE", default="English")

        # Legacy backward-compatibility mapping
        self.anthropic_api_key = self.ai_api_key
        self.anthropic_model = self.ai_model

        self.telegram_bot_token = cfg("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = cfg("TELEGRAM_CHAT_ID")
        self.groq_api_key = cfg("GROQ_API_KEY")
        # Legacy readers (src/agent/tools.py) still look at os.environ.
        if self.telegram_bot_token:
            os.environ["TELEGRAM_BOT_TOKEN"] = self.telegram_bot_token
        if self.telegram_chat_id:
            os.environ["TELEGRAM_CHAT_ID"] = self.telegram_chat_id

    # Read-only compatibility aliases for callers outside this module
    # (main.py, src/app.py, settings_window.py). Writes are ignored.
    @property
    def ai_provider(self) -> str:
        return "anthropic"

    @property
    def ai_base_url(self) -> str:
        return ANTHROPIC_API_URL

    @ai_base_url.setter
    def ai_base_url(self, _value: str) -> None:
        pass

    @property
    def anthropic_base_url(self) -> str:
        return ANTHROPIC_API_URL

    @anthropic_base_url.setter
    def anthropic_base_url(self, _value: str) -> None:
        pass

    def get_config_dict(self) -> Dict[str, Any]:
        """Return masked dictionary of the active Claude configuration."""
        has_key = bool(self.ai_api_key)
        masked_key = ""
        if has_key:
            if len(self.ai_api_key) > 8:
                masked_key = f"{self.ai_api_key[:4]}••••••••{self.ai_api_key[-4:]}"
            else:
                masked_key = "••••••••"

        return {
            "model": self.ai_model,
            "api_key": masked_key,
            "has_api_key": has_key,
            "language": self.altron_language,
        }
