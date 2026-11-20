"""Piper voice discovery: which .onnx model speaks which language, and at what pace.

Resolution order for each language (explicit argument > .env > process env > default):

* English: ``PIPER_MODEL_PATH`` if set (must exist), else ``<data dir>/voices/en_US-ryan-high.onnx``.
* Arabic:  ``PIPER_MODEL_PATH_AR`` if set (must exist), else ``<data dir>/voices/ar_JO-kareem-medium.onnx``.

An empty string means "no Piper voice for this language", and TTS falls back to edge-tts.
Downloading the voices is an explicit user step (see ``scripts/`` / README). The app itself
never downloads models, so the only network hosts it talks to stay as audit S2 defines them.
"""
import math
from pathlib import Path

from src.config.log import get_logger
from src.config.paths import get_runtime_dir

logger = get_logger(__name__)

EN_VOICE = "en_US-joe-medium"
AR_VOICE = "ar_JO-kareem-medium"
DEFAULT_LENGTH_SCALE = 1.06  # slightly slower than neutral: deliberate, authoritative delivery
MIN_LENGTH_SCALE, MAX_LENGTH_SCALE = 0.5, 2.0


def default_voices_dir() -> Path:
    """Where ``python -m piper.download_voices`` puts the voices (inside the runtime dir)."""
    return get_runtime_dir() / "voices"


def default_model_file(lang: str) -> Path:
    voice = AR_VOICE if lang == "ar" else EN_VOICE
    return default_voices_dir() / f"{voice}.onnx"


def resolve_model_path(lang: str, explicit: str = "") -> str:
    """Return the .onnx path to use for ``lang`` ("en" or "ar"), or "" when none is usable."""
    if explicit:
        if Path(explicit).is_file():
            return str(explicit)
        logger.debug("Piper model for %s not found at %s; using edge-tts", lang, explicit)
        return ""
    default = default_model_file(lang)
    return str(default) if default.is_file() else ""


def parse_length_scale(value) -> float:
    """Clamp ``PIPER_LENGTH_SCALE`` to a sane range; anything unparseable gives the default."""
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return DEFAULT_LENGTH_SCALE
    if not math.isfinite(scale):
        return DEFAULT_LENGTH_SCALE
    return min(MAX_LENGTH_SCALE, max(MIN_LENGTH_SCALE, scale))


def language_for_text(text: str) -> str:
    """"ar" when the text contains Arabic script, otherwise "en"."""
    return "ar" if any("؀" <= c <= "ۿ" for c in text or "") else "en"
