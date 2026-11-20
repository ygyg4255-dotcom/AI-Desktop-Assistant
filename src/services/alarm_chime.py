"""Alarm chime for reminders: three rising, pulsing sci-fi tones built with numpy.

No audio files are shipped. ``build_chime`` is pure numpy (testable without a sound
card); ``play_alarm_chime`` is the only part that touches ``pygame.mixer``, and it
imports pygame lazily and returns False instead of raising when audio is unavailable.
"""
import math

import numpy as np

from src.config.log import get_logger

logger = get_logger(__name__)

DEFAULT_SAMPLE_RATE = 44100
PULSES = 3
PULSE_SECONDS = 0.22
GAP_SECONDS = 0.13
BASE_HZ = 1046.5      # C6
PEAK_HZ = 1568.0      # G6: each pulse sweeps up a fifth, which reads as an alert
OVERTONE = 0.35       # second harmonic adds the metallic edge
VOLUME = 0.6          # headroom so the chime never clips
ATTACK_SECONDS = 0.006
DECAY_SECONDS = 0.10


def _pulse(n: int, sample_rate: int) -> np.ndarray:
    """One pulse: a rising sweep with a second harmonic and a fast attack and exponential decay."""
    t = np.arange(n) / sample_rate
    duration = n / sample_rate
    freq = BASE_HZ + (PEAK_HZ - BASE_HZ) * (t / duration)
    phase = 2.0 * math.pi * np.cumsum(freq) / sample_rate
    tone = np.sin(phase) + OVERTONE * np.sin(2.0 * phase)
    attack = np.minimum(1.0, t / ATTACK_SECONDS)
    decay = np.exp(-t / DECAY_SECONDS)
    return tone * attack * decay


def build_chime(sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = 2,
                pulses: int = PULSES) -> np.ndarray:
    """Return the chime as int16 PCM: shape (n,) for mono, (n, 2) for stereo.

    Pulses are separated by silent gaps so the alarm reads as *pulsing* rather than a drone.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if channels not in (1, 2):
        raise ValueError("channels must be 1 or 2")
    pulses = max(1, int(pulses))

    pulse_n = int(PULSE_SECONDS * sample_rate)
    gap_n = int(GAP_SECONDS * sample_rate)
    pulse = _pulse(pulse_n, sample_rate)
    mono = np.zeros(pulses * pulse_n + (pulses - 1) * gap_n, dtype=np.float64)
    for i in range(pulses):
        start = i * (pulse_n + gap_n)
        mono[start:start + pulse_n] = pulse

    peak = float(np.max(np.abs(mono))) or 1.0
    mono = mono / peak * VOLUME
    pcm = np.clip(np.round(mono * 32767), -32768, 32767).astype(np.int16)
    if channels == 2:
        return np.column_stack([pcm, pcm])
    return pcm


def chime_duration_seconds(pulses: int = PULSES) -> float:
    """Length of the chime in seconds (used to wait for it to finish)."""
    pulses = max(1, int(pulses))
    return pulses * PULSE_SECONDS + (pulses - 1) * GAP_SECONDS


def play_alarm_chime(wait: bool = True) -> bool:
    """Play the chime on the default output. Returns True if it was started.

    Never raises: a missing or busy audio device is logged at DEBUG and reported as False.
    With ``wait=True`` the call returns after the chime has finished playing.
    """
    try:
        import time
        import pygame
        import pygame.sndarray  # noqa: F401

        if not pygame.mixer.get_init():
            pygame.mixer.init()
        freq, _fmt, channels = pygame.mixer.get_init()
        sound = pygame.sndarray.make_sound(build_chime(sample_rate=int(freq), channels=int(channels)))
        sound.play()
        if wait:
            time.sleep(chime_duration_seconds())
        return True
    except Exception as err:
        logger.debug("Alarm chime unavailable: %s", err)
        return False
