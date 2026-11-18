"""Application settings and configuration with Multi-Provider AI support."""
import os
import sys
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from dotenv import load_dotenv

load_dotenv(override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def load_env_file(env_path: Path) -> dict:
    """Load environment variables from .env file."""
    env_vars = {}
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip()
    return env_vars


def is_masked_key(key: str) -> bool:
    """Check if API key is a masked placeholder and should not overwrite existing secret."""
    if not key or not str(key).strip():
        return True
    s = str(key).strip()
    return "•" in s or "*" in s or s == "••••••••" or (len(s) > 4 and "••" in s)


def save_ai_config(
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    language: str = "English",
    env_path: Optional[Path] = None
) -> Dict[str, str]:
    """Persist AI provider configuration cleanly to .env and system environment."""
    if env_path is None:
        if getattr(sys, "frozen", False):
            env_path = Path(sys.executable).parent / ".env"
        else:
            env_path = Path(__file__).resolve().parent.parent.parent / ".env"

    updates = {
        "AI_PROVIDER": provider.strip(),
        "AI_MODEL": model.strip(),
        "AI_BASE_URL": base_url.strip(),
        "ALTRON_LANGUAGE": language.strip(),
        "ANTHROPIC_MODEL": model.strip(),
        "ANTHROPIC_BASE_URL": base_url.strip(),
    }
    if not is_masked_key(api_key):
        updates["AI_API_KEY"] = api_key.strip()
        updates["ANTHROPIC_API_KEY"] = api_key.strip()

    lines = []
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            lines = []

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
    except Exception:
        pass

    for k, v in updates.items():
        os.environ[k] = str(v)

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

    # Universal AI Provider settings
    ai_provider: str = "anthropic"
    ai_model: str = "claude-3-5-sonnet-20241022"
    ai_api_key: str = ""
    ai_base_url: str = "https://api.anthropic.com"
    altron_language: str = "English"

    # Legacy settings mapped to Universal provider
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-sonnet-20241022"
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

        self.ai_provider = env_vars.get(
            "AI_PROVIDER",
            os.environ.get("AI_PROVIDER", "anthropic")
        ).lower().strip()

        self.ai_model = env_vars.get(
            "AI_MODEL",
            env_vars.get("ANTHROPIC_MODEL", os.environ.get("AI_MODEL", os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")))
        )

        self.ai_api_key = env_vars.get(
            "AI_API_KEY",
            env_vars.get("ANTHROPIC_API_KEY", env_vars.get("OPENAI_API_KEY", os.environ.get("AI_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))))
        )

        self.ai_base_url = env_vars.get(
            "AI_BASE_URL",
            env_vars.get("ANTHROPIC_BASE_URL", os.environ.get("AI_BASE_URL", os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")))
        )

        self.altron_language = env_vars.get(
            "ALTRON_LANGUAGE",
            os.environ.get("ALTRON_LANGUAGE", "English")
        )

        # Legacy backward-compatibility mapping
        self.anthropic_base_url = self.ai_base_url
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

    def get_config_dict(self) -> Dict[str, Any]:
        """Return masked dictionary of active AI provider configuration."""
        has_key = bool(self.ai_api_key)
        masked_key = ""
        if has_key:
            if len(self.ai_api_key) > 8:
                masked_key = f"{self.ai_api_key[:4]}••••••••{self.ai_api_key[-4:]}"
            else:
                masked_key = "••••••••"

        return {
            "provider": self.ai_provider,
            "model": self.ai_model,
            "base_url": self.ai_base_url,
            "api_key": masked_key,
            "has_api_key": has_key,
            "language": self.altron_language,
        }
