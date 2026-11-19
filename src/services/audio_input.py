"""Microphone-only input helpers: loopback filtering, echo gate, energy floor.

Pure functions and small classes with no audio-library imports, so the voice loop
can use them and tests can exercise them without hardware.
"""
import re
import time
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from src.config.log import get_logger

logger = get_logger(__name__)

# Device names that capture what the PC is playing instead of a real microphone.
LOOPBACK_MARKERS = ("stereo mix", "loopback", "what u hear", "what you hear", "wave out", "monitor")

# Silence enforced after TTS stops, so speaker reverb and buffered echo are not heard.
POST_TTS_COOLDOWN_SECONDS = 0.8

# Utterances whose active speech is quieter than this are background noise; sending
# them to Whisper makes it hallucinate "Thank you." / "I'm sorry." (float scale 0..1).
MIN_UTTERANCE_RMS = 0.012

_MIC_WORD = re.compile(r"(^|[^a-z])(microphone|mic)([^a-z]|$)", re.IGNORECASE)

FRAME_SAMPLES = 480  # 30 ms at 16 kHz


def is_loopback_device(name: str) -> bool:
    lowered = (name or "").lower()
    return any(marker in lowered for marker in LOOPBACK_MARKERS)


def pick_input_device(devices: Sequence[Dict], preferred: str = "",
                      default_index: Optional[int] = None) -> Optional[int]:
    """Choose the index of a real microphone.

    ``devices`` items are dicts with ``index``, ``name`` and ``max_input_channels``.
    An explicit ``preferred`` (device index or name fragment, i.e. ALTRON_MIC_DEVICE)
    wins when it matches an input device. Otherwise the system default input is used
    unless it is a loopback device, then the first non-loopback input (microphone-like
    names first). Returns None when nothing suitable exists.
    """
    inputs: List[Dict] = [d for d in devices if int(d.get("max_input_channels", 0) or 0) > 0]
    wanted = (preferred or "").strip()
    if wanted:
        if wanted.isdigit():
            for d in inputs:
                if d["index"] == int(wanted):
                    return d["index"]
        else:
            for d in inputs:
                if wanted.lower() in d["name"].lower():
                    return d["index"]
        logger.warning("ALTRON_MIC_DEVICE %r matched no input device; auto-selecting", wanted)

    real = [d for d in inputs if not is_loopback_device(d["name"])]
    if default_index is not None and any(d["index"] == default_index for d in real):
        return default_index
    for d in real:
        if _MIC_WORD.search(d["name"]):
            return d["index"]
    return real[0]["index"] if real else None


def frame_rms(frame_bytes: bytes) -> float:
    samples = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0


def active_speech_rms(pcm: bytes, frame_samples: int = FRAME_SAMPLES) -> float:
    """RMS of the loud 30 ms frames (within ~6 dB of the peak), so silence padding is ignored."""
    samples = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype=np.int16).astype(np.float32) / 32768.0
    usable = (samples.size // frame_samples) * frame_samples
    if usable == 0:
        return float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
    frames = samples[:usable].reshape(-1, frame_samples)
    energy = np.mean(frames ** 2, axis=1)
    loud = energy[energy >= 0.25 * energy.max()]  # frames within ~6 dB of the loudest
    return float(np.sqrt(np.mean(loud)))


def is_too_quiet(pcm: bytes, min_rms: float = MIN_UTTERANCE_RMS) -> bool:
    return active_speech_rms(pcm) < min_rms


class EchoGate:
    """Tells the capture loop when to discard frames (TTS playing, paused, or cooling down).

    The loop must keep *reading* frames while the gate is closed so the OS input
    buffer never holds audio recorded during playback.
    """

    def __init__(self, cooldown: float = POST_TTS_COOLDOWN_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self._cooldown = cooldown
        self._clock = clock
        self._was_blocked = False
        self._until = 0.0

    def should_discard(self, blocked: bool) -> bool:
        """``blocked`` is True while TTS is speaking or the listener is paused."""
        now = self._clock()
        if blocked:
            self._was_blocked = True
            return True
        if self._was_blocked:
            self._was_blocked = False
            self._until = now + self._cooldown
        return now < self._until
