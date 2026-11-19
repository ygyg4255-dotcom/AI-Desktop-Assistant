"""Lightweight numpy-only voice effects (the "Ultron" metallic chassis resonance)."""
import numpy as np

from src.config.log import get_logger

logger = get_logger(__name__)

FX_NONE = "none"
FX_ULTRON = "ultron"

COMB_DELAY_S = 0.014   # ~14 ms short metallic comb
COMB_WET = 0.22        # ~22% wet
WARMTH_CUTOFF_HZ = 250.0
WARMTH_GAIN = 0.30     # gentle low-end boost
PEAK_LIMIT = 0.97


def normalize_fx_name(value) -> str:
    """Map a config value to a known effect name (unknown/empty -> default ultron)."""
    name = str(value if value is not None else FX_ULTRON).strip().lower()
    if name in ("", "default"):
        return FX_ULTRON
    if name in ("none", "off", "false", "0", "no"):
        return FX_NONE
    return name if name == FX_ULTRON else FX_ULTRON


def _ultron_mono(x: np.ndarray, sample_rate: int) -> np.ndarray:
    delay = max(1, int(round(COMB_DELAY_S * sample_rate)))
    wet = np.zeros_like(x)
    if len(x) > delay:
        wet[delay:] = x[:-delay]
    y = x + COMB_WET * wet

    # Moving-average low-pass (vectorised) -> gentle low-end warmth
    win = max(2, int(sample_rate / WARMTH_CUTOFF_HZ))
    csum = np.cumsum(np.insert(y, 0, 0.0))
    idx = np.arange(len(y))
    start = np.maximum(0, idx - win + 1)
    low = (csum[idx + 1] - csum[start]) / (idx - start + 1)
    return y + WARMTH_GAIN * low


def apply_voice_fx(samples: np.ndarray, sample_rate: int, fx: str = FX_ULTRON) -> np.ndarray:
    """Apply the named effect to int16 PCM (shape (n,) or (n, channels)).

    Output has the same shape, dtype and duration as the input. Never raises:
    on any failure (or ``fx`` of none) the original samples are returned.
    """
    try:
        if normalize_fx_name(fx) == FX_NONE:
            return samples
        arr = np.asarray(samples)
        if arr.size == 0 or sample_rate <= 0:
            return samples
        in_dtype = arr.dtype
        x = arr.astype(np.float64)
        scale = 32768.0 if np.issubdtype(in_dtype, np.integer) else 1.0
        x = x / scale
        if x.ndim == 1:
            y = _ultron_mono(x, sample_rate)
        else:
            y = np.stack([_ultron_mono(x[:, c], sample_rate) for c in range(x.shape[1])], axis=1)
        peak = float(np.max(np.abs(y)))
        if peak > PEAK_LIMIT:
            y = y * (PEAK_LIMIT / peak)
        y = y * scale
        if np.issubdtype(in_dtype, np.integer):
            y = np.clip(np.rint(y), np.iinfo(in_dtype).min, np.iinfo(in_dtype).max)
        return y.astype(in_dtype)
    except Exception as err:
        logger.debug("voice fx failed, using dry audio: %s", err)
        return samples
