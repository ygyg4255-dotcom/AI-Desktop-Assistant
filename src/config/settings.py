"""Application settings and configuration (Anthropic Claude only)."""
import os
import sys
import json
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from dotenv import load_dotenv

load_dotenv(override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# The API key is only ever sent here. Deliberately not configurable (audit S2).
ANTHROPIC_API_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_FAST_MODEL = "claude-haiku-5-5"


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
        if getattr(sys, "frozen", False):
            env_path = Path(sys.executable).parent / ".env"
        else:
            env_path = Path(__file__).resolve().parent.parent.parent / ".env"

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
            print(f"[Config] Could not read {env_path.name}: {type(err).__name__}")
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
        print(f"[Config] Failed to write {env_path.name}: {type(err).__name__}")
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

        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).parent
            if (exe_dir / ".env").exists():
                self.env_path = exe_dir / ".env"
            elif (self.data_dir / ".env").exists():
                self.env_path = self.data_dir / ".env"
            else:
                self.env_path = exe_dir / ".env"
        else:
            self.env_path = Path(__file__).resolve().parent.parent.parent / ".env"

        self.load_environment()

    def load_environment(self):
        """Load environment variables from .env file and system env."""
        env_vars = load_env_file(self.env_path)

        model = env_vars.get(
            "AI_MODEL",
            env_vars.get("ANTHROPIC_MODEL", os.environ.get("AI_MODEL", os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)))
        ).strip()
        # "serve" was the old OmniRoute proxy placeholder, not a Claude model.
        self.ai_model = DEFAULT_MODEL if model in ("", "serve") else model

        self.ai_fast_model = env_vars.get(
            "AI_FAST_MODEL", os.environ.get("AI_FAST_MODEL", DEFAULT_FAST_MODEL)
        ).strip() or DEFAULT_FAST_MODEL

        self.ai_api_key = env_vars.get(
            "AI_API_KEY",
            env_vars.get("ANTHROPIC_API_KEY", os.environ.get("AI_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "")))
        )

        self.altron_language = env_vars.get(
            "ALTRON_LANGUAGE",
            os.environ.get("ALTRON_LANGUAGE", "English")
        )

        # Legacy backward-compatibility mapping
        self.anthropic_api_key = self.ai_api_key
        self.anthropic_model = self.ai_model

        self.telegram_bot_token = env_vars.get(
            "TELEGRAM_BOT_TOKEN",
            os.environ.get("TELEGRAM_BOT_TOKEN", self.telegram_bot_token)
        )
        self.telegram_chat_id = env_vars.get(
            "TELEGRAM_CHAT_ID",
            os.environ.get("TELEGRAM_CHAT_ID", self.telegram_chat_id)
        )
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
