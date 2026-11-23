"""Legacy QThread wrappers (deprecated src/ui/ only). Importing this module loads PySide6;
the headless runtime (main.py) must never import it."""
from typing import Callable, List, Optional

from PySide6.QtCore import QThread, Signal

from src.services.telegram_service import TelegramRemoteListener
from src.services.voice_service import SpeechRecognitionListener


class SpeechRecognitionWorker(QThread, SpeechRecognitionListener):
    """Legacy Qt wrapper (deprecated src/ui/ only): same loop, real Qt signals.

    Start it with ``start()``; never call ``run()`` on it from a Python thread.
    """

    started_listening = Signal()
    status_updated = Signal(str)
    text_recognized = Signal(str)
    error_occurred = Signal(str)
    finished_listening = Signal()

    def __init__(self, languages: Optional[List[str]] = None, model_size: Optional[str] = None, parent=None):
        QThread.__init__(self, parent)
        SpeechRecognitionListener.__init__(self, languages, model_size)

    def run(self) -> None:
        SpeechRecognitionListener.run(self)


class TelegramRemoteWorker(QThread, TelegramRemoteListener):
    """Legacy Qt wrapper (deprecated src/ui/ only): same loop, a real Qt signal.

    Start it with ``start()``; never call ``run()`` on it from a Python thread.
    """

    remote_command_received = Signal(str)

    def __init__(self, bot_token: Optional[str] = None, allowed_chat_id: Optional[str] = None, parent=None,
                 callback_handler: Optional[Callable[[dict], object]] = None):
        QThread.__init__(self, parent)
        TelegramRemoteListener.__init__(self, bot_token, allowed_chat_id, callback_handler)

    def run(self) -> None:
        TelegramRemoteListener.run(self)
