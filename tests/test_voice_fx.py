import numpy as np
import pytest

from src.services import voice_fx
from src.services.voice_fx import apply_voice_fx, normalize_fx_name

RATE = 24000


def _tone(seconds=0.5, channels=1):
    t = np.arange(int(RATE * seconds)) / RATE
    wave = (0.5 * np.sin(2 * np.pi * 180 * t) * 32767).astype(np.int16)
    return wave if channels == 1 else np.stack([wave] * channels, axis=1)


@pytest.mark.parametrize("channels", [1, 2])
def test_ultron_preserves_shape_dtype_and_changes_audio(channels):
    dry = _tone(channels=channels)
    wet = apply_voice_fx(dry, RATE, "ultron")
    assert wet.shape == dry.shape
    assert wet.dtype == dry.dtype
    assert wet.size > 0
    assert np.any(wet != dry)
    assert np.max(np.abs(wet.astype(np.int32))) <= 32767
    assert np.any(wet != 0)


def test_loud_input_does_not_clip_or_wrap():
    dry = np.full(RATE // 2, 32767, dtype=np.int16)
    wet = apply_voice_fx(dry, RATE, "ultron")
    assert wet.min() > 0  # no int16 wraparound to negative


def test_none_bypasses_filter():
    dry = _tone()
    assert apply_voice_fx(dry, RATE, "none") is dry


def test_empty_input_returned_untouched():
    dry = np.zeros(0, dtype=np.int16)
    assert apply_voice_fx(dry, RATE, "ultron") is dry


def test_failure_falls_back_to_dry_audio(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("dsp exploded")

    monkeypatch.setattr(voice_fx, "_ultron_mono", boom)
    dry = _tone()
    assert apply_voice_fx(dry, RATE, "ultron") is dry


def test_normalize_fx_name():
    assert normalize_fx_name(None) == "ultron"
    assert normalize_fx_name("") == "ultron"
    assert normalize_fx_name("ULTRON") == "ultron"
    assert normalize_fx_name("none") == "none"
    assert normalize_fx_name("off") == "none"
    assert normalize_fx_name("bogus") == "ultron"


def test_settings_voice_fx_toggle(monkeypatch, tmp_path):
    from src.config.settings import Settings

    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALTRON_VOICE_FX", "none")
    assert Settings().voice_fx == "none"
    monkeypatch.setenv("ALTRON_VOICE_FX", "ultron")
    assert Settings().voice_fx == "ultron"
