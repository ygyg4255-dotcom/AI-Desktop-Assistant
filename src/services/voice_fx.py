"""Numpy-only "Ultron" vocal chain applied to TTS audio.

Chain (mono, float in [-1, 1]; int16 in/out keeps shape, dtype and duration):

1. Chest weight: zero-phase spectral bell around ~130 Hz (one FFT pair) plus a sub-bass
   layer. The sub layer is a waveshaped, smoothed low band. It approximates sub-harmonic
   weight but is not a true octave-down pitch shift, which would change the duration.
2. Metallic chassis: three short feed-forward comb approximations (5 / 8.5 / 12.5 ms,
   feedback 0.3 truncated after 4 repeats), mixed at about 25% wet.
3. Synthetic edge: ~45 Hz ring-modulation sheen at ~9% and gentle tanh saturation.
4. Presence: high shelf lifting from ~2.5 kHz so consonants stay crisp.
5. Soft limiter with a knee at 0.8, then a hard safety ceiling.

Everything is vectorised numpy (FFT, cumsum or shift), with no per-sample Python loop.
"""
from functools import lru_cache

import numpy as np

from src.config.log import get_logger

logger = get_logger(__name__)

FX_NONE = "none"
FX_ULTRON = "ultron"

# 1. Chest weight
CHEST_CENTER_HZ = 130.0
CHEST_GAIN = 0.45        # about +3.5 dB at the centre of the bell
CHEST_OCTAVES = 0.9      # bell width (sigma, in octaves)
SUB_CUTOFF_HZ = 110.0
SUB_DRIVE = 2.0
SUB_MIX = 0.12

# 2. Metallic chassis combs
COMB_DELAYS_S = (0.005, 0.0085, 0.0125)
COMB_FEEDBACK = 0.30
COMB_REPEATS = 4         # feedback repeats kept; 0.3**5 is about 0.002, negligible
COMB_WET = 0.25

# 3. Synthetic edge
RING_HZ = 45.0
RING_MIX = 0.09
SAT_DRIVE = 1.2
SAT_MIX = 0.20

# 4. Presence
PRESENCE_CENTER_HZ = 2500.0
PRESENCE_GAIN = 0.35     # about +2.8 dB above the centre frequency

# 5. Limiter
LIMIT_KNEE = 0.80
PEAK_LIMIT = 0.97


def normalize_fx_name(value) -> str:
    """Map a config value to a known effect name (unknown/empty -> default ultron)."""
    name = str(value if value is not None else FX_ULTRON).strip().lower()
    if name in ("", "default"):
        return FX_ULTRON
    if name in ("none", "off", "false", "0", "no"):
        return FX_NONE
    return name if name == FX_ULTRON else FX_ULTRON


def _bell(freqs: np.ndarray, center: float, gain: float, octaves: float) -> np.ndarray:
    """Gain curve 1 + gain * gaussian(log2 distance), so it only boosts around ``center``."""
    octs = np.log2(np.maximum(freqs, 1.0) / center)
    return 1.0 + gain * np.exp(-0.5 * (octs / octaves) ** 2)


@lru_cache(maxsize=16)
def _chest_gain(n: int, sample_rate: int) -> np.ndarray:
    """Spectral gain curve for the chest bell; cached per (length, rate)."""
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    gain = _bell(freqs, CHEST_CENTER_HZ, CHEST_GAIN, CHEST_OCTAVES)
    gain.setflags(write=False)
    return gain


def _chest_eq(x: np.ndarray, sample_rate: int) -> np.ndarray:
    """Zero-phase bass bell via one FFT pair."""
    return np.fft.irfft(np.fft.rfft(x) * _chest_gain(len(x), sample_rate), n=len(x))


def _boxcar(x: np.ndarray, width: int) -> np.ndarray:
    """Centred (zero-phase) moving average, O(n) via cumsum. Cheap low-band smoother."""
    width = max(1, int(width))
    if width == 1 or len(x) == 0:
        return x.copy()
    half = width // 2
    padded = np.concatenate((np.zeros(half), x, np.zeros(width - half)))
    csum = np.concatenate(([0.0], np.cumsum(padded)))
    return (csum[width:width + len(x)] - csum[:len(x)]) / width  # slices only, no gathers


def _shift(x: np.ndarray, samples: int) -> np.ndarray:
    """Delay ``x`` by ``samples`` (zero-filled), without wrapping."""
    out = np.zeros_like(x)
    if 0 < samples < len(x):
        out[samples:] = x[:-samples]
    return out


def _comb_wet(x: np.ndarray, delay: int) -> np.ndarray:
    """Wet part of a feedback comb: sum of g**k * x[n - k*delay] for k = 1..COMB_REPEATS."""
    wet = np.zeros_like(x)
    gain = 1.0
    for k in range(1, COMB_REPEATS + 1):
        gain *= COMB_FEEDBACK
        wet += gain * _shift(x, k * delay)
    return wet


def _chest_and_sub(x: np.ndarray, sample_rate: int) -> np.ndarray:
    y = _chest_eq(x, sample_rate)
    width = sample_rate / SUB_CUTOFF_HZ
    low = _boxcar(y, width)
    sub = _boxcar(np.tanh(SUB_DRIVE * low), width)
    return y + SUB_MIX * sub


def _metallic_chassis(x: np.ndarray, sample_rate: int) -> np.ndarray:
    wets = [_comb_wet(x, max(1, int(round(d * sample_rate)))) for d in COMB_DELAYS_S]
    return x + COMB_WET * (sum(wets) / len(wets))


@lru_cache(maxsize=16)
def _ring_modulator(n: int, sample_rate: int) -> np.ndarray:
    t = np.arange(n) / float(sample_rate)
    mod = 1.0 - RING_MIX + RING_MIX * np.sin(2.0 * np.pi * RING_HZ * t)
    mod.setflags(write=False)
    return mod


def _synthetic_edge(x: np.ndarray, sample_rate: int) -> np.ndarray:
    y = x * _ring_modulator(len(x), sample_rate)
    saturated = np.tanh(SAT_DRIVE * y) / np.tanh(SAT_DRIVE)  # unity at full scale
    return (1.0 - SAT_MIX) * y + SAT_MIX * saturated


def _presence(x: np.ndarray, sample_rate: int) -> np.ndarray:
    """High shelf that starts lifting around PRESENCE_CENTER_HZ (x - boxcar(x) is high-passed).

    The boxcar's first null sits at the centre frequency, so the lift is about
    +PRESENCE_GAIN above it and unity below it.
    """
    width = max(1, int(round(sample_rate / PRESENCE_CENTER_HZ)))
    return x + PRESENCE_GAIN * (x - _boxcar(x, width))


def _soft_limit(x: np.ndarray) -> np.ndarray:
    """Pass |x| <= knee untouched, then bend the excess smoothly towards 1.0."""
    span = 1.0 - LIMIT_KNEE
    out = x.copy()
    over = np.abs(x) > LIMIT_KNEE
    if np.any(over):
        vals = x[over]
        excess = (np.abs(vals) - LIMIT_KNEE) / span
        out[over] = np.sign(vals) * (LIMIT_KNEE + span * np.tanh(excess))
    return out


def _ultron_mono(x: np.ndarray, sample_rate: int) -> np.ndarray:
    y = _chest_and_sub(x, sample_rate)
    y = _metallic_chassis(y, sample_rate)
    y = _synthetic_edge(y, sample_rate)
    y = _presence(y, sample_rate)
    y = _soft_limit(y)
    return np.clip(y, -PEAK_LIMIT, PEAK_LIMIT)


def apply_voice_fx(samples: np.ndarray, sample_rate: int, fx: str = FX_ULTRON) -> np.ndarray:
    """Apply the named effect to PCM (shape (n,) or (n, channels)).

    Integer input is treated as full scale and returned as the same integer dtype.
    Output has the same shape, dtype and duration as the input. Never raises: on any
    failure (or ``fx`` of none) the original samples are returned unchanged.
    """
    try:
        if normalize_fx_name(fx) == FX_NONE:
            return samples
        arr = np.asarray(samples)
        if arr.size == 0 or sample_rate <= 0:
            return samples
        in_dtype = arr.dtype
        is_int = np.issubdtype(in_dtype, np.integer)
        scale = 32768.0 if is_int else 1.0
        x = arr.astype(np.float64) / scale
        if x.ndim == 1:
            y = _ultron_mono(x, sample_rate)
        else:
            y = np.stack([_ultron_mono(np.ascontiguousarray(x[:, c]), sample_rate)
                          for c in range(x.shape[1])], axis=1)
        y = y * scale
        if is_int:
            info = np.iinfo(in_dtype)
            y = np.clip(np.rint(y), info.min, info.max)
        return y.astype(in_dtype)
    except Exception as err:
        logger.debug("voice fx failed, using dry audio: %s", err)
        return samples
