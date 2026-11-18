"""Services package."""
from src.services.database import DatabaseService
from src.services.ai_client import AIClient, Message, AIResponse, ConnectionStatus
from src.services.omniroute_manager import OmniRouteManager
from src.services.voice_service import (
    TTSManager,
    SpeechRecognitionListener,
    SpeechRecognitionWorker,
    clean_text_for_speech,
    parse_wake_word,
    is_speaking_event,
)
from src.services.desktop_icon_service import position_desktop_icon, get_desktop_icons
from src.services.telegram_service import (
    TelegramRemoteListener,
    TelegramRemoteWorker,
    send_telegram_message,
    send_telegram_message_async,
    send_telegram_document,
    send_telegram_photo,
    send_telegram_photo_async,
)

__all__ = [
    "DatabaseService",
    "AIClient",
    "Message",
    "AIResponse",
    "ConnectionStatus",
    "OmniRouteManager",
    "TTSManager",
    "SpeechRecognitionListener",
    "SpeechRecognitionWorker",
    "clean_text_for_speech",
    "parse_wake_word",
    "is_speaking_event",
    "position_desktop_icon",
    "get_desktop_icons",
    "TelegramRemoteListener",
    "TelegramRemoteWorker",
    "send_telegram_message",
    "send_telegram_message_async",
    "send_telegram_document",
    "send_telegram_photo",
    "send_telegram_photo_async",
]
