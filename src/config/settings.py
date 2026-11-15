"""Application settings and configuration."""
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
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


@dataclass
class Settings:
    """Application settings loaded from configuration."""

    app_name: str = "AI Desktop Assistant"
    app_version: str = "0.2.0"
    app_author: str = "AI Desktop Assistant"

    # Paths
    data_dir: Path = field(default_factory=lambda: Path.home() / ".ai-desktop-assistant")
    db_path: Path = field(init=False)
    config_path: Path = field(init=False)
    env_path: Path = field(init=False)

    # Window settings
    window_width: int = 600
    window_height: int = 700

    # AI Client settings (loaded from .env)
    anthropic_base_url: str = "http://localhost:20128"
    anthropic_api_key: str = ""
    anthropic_model: str = "serve"
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

        # Resolve .env path for both source and frozen PyInstaller builds
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

        # Load .env file
        self._load_environment()

    def _load_environment(self):
        """Load environment variables from .env file."""
        env_vars = load_env_file(self.env_path)

        # Also check system environment (for overrides)
        self.anthropic_base_url = env_vars.get(
            "ANTHROPIC_BASE_URL",
            os.environ.get("ANTHROPIC_BASE_URL", self.anthropic_base_url)
        )
        self.anthropic_api_key = env_vars.get(
            "ANTHROPIC_API_KEY",
            os.environ.get("ANTHROPIC_API_KEY", "")
        )
        self.anthropic_model = env_vars.get(
            "ANTHROPIC_MODEL",
            os.environ.get("ANTHROPIC_MODEL", self.anthropic_model)
        )
        self.telegram_bot_token = env_vars.get(
            "TELEGRAM_BOT_TOKEN",
            os.environ.get("TELEGRAM_BOT_TOKEN", self.telegram_bot_token)
        )
        self.telegram_chat_id = env_vars.get(
            "TELEGRAM_CHAT_ID",
            os.environ.get("TELEGRAM_CHAT_ID", self.telegram_chat_id)
        )
