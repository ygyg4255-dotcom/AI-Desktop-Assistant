"""Spoken/typed control commands: shut down, mute/resume the mic, forget memory.

Matching is deliberately strict: the WHOLE utterance must be one of the known
phrases (after dropping filler such as "please" and an "Altron" address). A longer
sentence that merely contains a phrase never triggers a control action, so noise
transcripts and normal requests cannot shut Altron down by accident.
"""
import string
from typing import Callable, Optional

from src.config.log import get_logger

logger = get_logger(__name__)

SHUTDOWN = "shutdown"
MUTE = "mute"
UNMUTE = "unmute"
CLEAR_MEMORY = "clear_memory"
CAMERA_START = "camera_start"
CAMERA_STOP = "camera_stop"

_LEADING_FILLER = {"hey", "ok", "okay", "please", "يا", "من", "فضلك"}
_TRAILING_FILLER = {"please", "now", "فضلك"}

_PHRASES = {
    SHUTDOWN: {
        "close yourself", "shut yourself down", "shut yourself off", "turn yourself off",
        "quit yourself", "shut down altron", "shutdown altron", "close altron", "quit altron",
        "exit altron", "turn off altron", "stop altron",
        "اغلق نفسك", "اطفئ نفسك", "اغلق ألترون", "اطفئ ألترون",
    },
    MUTE: {
        "mute yourself", "mute mic", "mute the mic", "mute microphone", "mute the microphone",
        "mute listening", "stop listening", "pause listening", "stop listening to me",
        "توقف عن الاستماع", "اوقف الاستماع", "اكتم الميكروفون", "كتم الميكروفون",
    },
    UNMUTE: {
        "unmute yourself", "unmute mic", "unmute the mic", "unmute microphone",
        "unmute the microphone", "resume listening", "start listening", "start listening again",
        "listen again", "استمع مجددا", "استأنف الاستماع", "الغ كتم الميكروفون",
    },
    CLEAR_MEMORY: {
        "clear memory", "clear your memory", "clear conversation history", "clear history",
        "forget previous session", "forget the previous session", "forget last session",
        "forget the last session", "forget everything", "forget our previous conversation",
        "امسح الذاكرة", "انس الجلسة السابقة",
    },
    CAMERA_START: {
        "start camera", "start the camera", "start webcam", "start the webcam",
        "turn on camera", "turn on the camera", "turn camera on", "turn on webcam",
        "turn on the webcam", "camera on", "شغل الكاميرا", "افتح الكاميرا",
    },
    CAMERA_STOP: {
        "stop camera", "stop the camera", "stop webcam", "stop the webcam",
        "turn off camera", "turn off the camera", "turn camera off", "turn off webcam",
        "turn off the webcam", "camera off", "اطفئ الكاميرا", "اوقف الكاميرا", "اغلق الكاميرا",
    },
}
# Short forms that are only unambiguous when addressed to Altron ("Altron, shut down").
_ADDRESSED_ONLY = {SHUTDOWN: {"shut down", "shutdown", "close", "quit", "exit", "اغلق", "اطفئ"}}

_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"})
_PUNCT = str.maketrans({c: " " for c in string.punctuation + "،؟؛!?.,…\"'“”"})


def _normalize(text: str) -> list:
    folded = (text or "").lower().translate(_PUNCT)
    return folded.translate(_ARABIC_FOLD).split()


_NORMALIZED = {
    action: {" ".join(_normalize(p)) for p in phrases} for action, phrases in _PHRASES.items()
}
_ADDRESSED = {
    action: {" ".join(_normalize(p)) for p in phrases} for action, phrases in _ADDRESSED_ONLY.items()
}
_NAME_TOKENS = {" ".join(_normalize(n)) for n in ("altron", "ألترون")}
_LEADING = {" ".join(_normalize(f)) for f in _LEADING_FILLER}
_TRAILING = {" ".join(_normalize(f)) for f in _TRAILING_FILLER}


def match_control_intent(text: str) -> Optional[str]:
    """Return SHUTDOWN / MUTE / UNMUTE / CLEAR_MEMORY / CAMERA_START / CAMERA_STOP, or None for anything else."""
    tokens = _normalize(text)
    while tokens and tokens[0] in _LEADING:
        tokens = tokens[1:]
    addressed = False
    if tokens and tokens[0] in _NAME_TOKENS:
        addressed, tokens = True, tokens[1:]
    while tokens and tokens[-1] in _TRAILING:
        tokens = tokens[:-1]
    if tokens and tokens[-1] in _NAME_TOKENS:
        addressed, tokens = True, tokens[:-1]
    phrase = " ".join(tokens)
    if not phrase:
        return None
    for action, phrases in _NORMALIZED.items():
        if phrase in phrases:
            return action
    if addressed:
        for action, phrases in _ADDRESSED.items():
            if phrase in phrases:
                return action
    return None


def _is_arabic(text: str) -> bool:
    return any("؀" <= ch <= "ۿ" for ch in text)


class ControlHandler:
    """Runs control actions through injected callables, so main.py wires real subsystems."""

    def __init__(self, clear_memory: Callable[[], None], request_shutdown: Callable[[], None],
                 pause_listening: Callable[[], None], resume_listening: Callable[[], None],
                 start_camera: Optional[Callable[[], bool]] = None,
                 stop_camera: Optional[Callable[[], None]] = None):
        self._start_camera = start_camera
        self._stop_camera = stop_camera
        self._clear_memory = clear_memory
        self._request_shutdown = request_shutdown
        self._pause = pause_listening
        self._resume = resume_listening
        self.muted = False

    def handle(self, text: str) -> Optional[str]:
        """Execute a matching control command and return the reply, or None if not a control command."""
        action = match_control_intent(text)
        if action is None:
            return None
        ar = _is_arabic(text)
        logger.info("Control command: %s", action)
        if action == SHUTDOWN:
            self._request_shutdown()
            return "حسنا، سأغلق نفسي الآن. إلى اللقاء." if ar else "Shutting down. Goodbye."
        if action == MUTE:
            self.muted = True
            self._pause()
            return ("تم كتم الميكروفون. ما زلت متاحا عبر الواجهة وتيليجرام." if ar else
                    "Microphone muted. I'm still available on the HUD and Telegram; say 'resume listening' there.")
        if action == UNMUTE:
            self.muted = False
            self._resume()
            return "عدت للاستماع." if ar else "Listening again."
        if action == CAMERA_START:
            if self._start_camera is not None and not self._start_camera():
                return "تعذر تشغيل الكاميرا." if ar else "I couldn't start the camera. Check that it's connected and not in use."
            return "تم تشغيل الكاميرا." if ar else "Camera on. I'll only look when you ask."
        if action == CAMERA_STOP:
            if self._stop_camera is not None:
                self._stop_camera()
            return "تم إيقاف الكاميرا." if ar else "Camera off."
        self._clear_memory()
        return "تم مسح ذاكرة الجلسة السابقة." if ar else "Done. I've cleared the saved conversation memory."
