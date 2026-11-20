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


# -- multi-stage Ultron chain ----------------------------------------------
def _sine(freq, seconds=0.4, amp=0.3):
    t = np.arange(int(RATE * seconds)) / RATE
    return amp * np.sin(2 * np.pi * freq * t)


def _rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


def test_chest_bell_boosts_bass_more_than_mids():
    bass = _rms(voice_fx._chest_eq(_sine(130), RATE)) / _rms(_sine(130))
    mid = _rms(voice_fx._chest_eq(_sine(1000), RATE)) / _rms(_sine(1000))
    assert bass > 1.25
    assert mid == pytest.approx(1.0, abs=0.05)


def test_presence_shelf_lifts_highs_and_leaves_lows_alone():
    high = _rms(voice_fx._presence(_sine(4000), RATE)) / _rms(_sine(4000))
    low = _rms(voice_fx._presence(_sine(300), RATE)) / _rms(_sine(300))
    assert high > 1.1
    assert low == pytest.approx(1.0, abs=0.05)


def test_comb_wet_has_decaying_echoes_at_multiples_of_delay():
    impulse = np.zeros(200)
    impulse[0] = 1.0
    wet = voice_fx._comb_wet(impulse, 10)
    assert wet[0] == 0.0  # the dry signal is added separately, not in the wet path
    assert wet[10] == pytest.approx(voice_fx.COMB_FEEDBACK)
    assert wet[20] == pytest.approx(voice_fx.COMB_FEEDBACK ** 2)
    assert wet[30] == pytest.approx(voice_fx.COMB_FEEDBACK ** 3)
    assert np.count_nonzero(wet) == voice_fx.COMB_REPEATS


def test_soft_limiter_never_exceeds_ceiling():
    x = np.linspace(-5, 5, 2001)
    y = voice_fx._soft_limit(x)
    assert np.all(np.abs(y[np.abs(x) <= voice_fx.LIMIT_KNEE]) == np.abs(x[np.abs(x) <= voice_fx.LIMIT_KNEE]))
    assert np.max(np.abs(y)) <= 1.0  # tanh saturates at exactly 1.0; the 0.97 ceiling is in _ultron_mono
    assert np.all(np.sign(y) == np.sign(x))


@pytest.mark.parametrize("dtype", [np.int16, np.float32])
def test_output_keeps_duration_and_dtype_for_mono_and_stereo(dtype):
    mono = _sine(180, seconds=1.0).astype(np.float64)
    if dtype == np.int16:
        mono = (mono * 32767).astype(np.int16)
    else:
        mono = mono.astype(np.float32)
    stereo = np.stack([mono, mono], axis=1)
    for arr in (mono, stereo):
        out = apply_voice_fx(arr, RATE, "ultron")
        assert out.shape == arr.shape
        assert out.dtype == arr.dtype
        assert np.all(np.isfinite(out.astype(np.float64)))


def test_full_scale_speech_like_input_is_limited_and_finite():
    t = np.arange(RATE) / RATE
    loud = (np.sign(np.sin(2 * np.pi * 150 * t)) * 32767).astype(np.int16)  # square wave, clips easily
    out = apply_voice_fx(loud, RATE, "ultron")
    assert np.max(np.abs(out.astype(np.int32))) <= int(32768 * voice_fx.PEAK_LIMIT) + 1


def test_chain_latency_sanity_for_two_seconds_of_speech_rate_audio():
    import time
    rate = 22050
    audio = (_sine(160, seconds=2.0) * 32767).astype(np.int16)
    apply_voice_fx(audio, rate, "ultron")  # warm caches / lru tables
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        apply_voice_fx(audio, rate, "ultron")
        best = min(best, time.perf_counter() - t0)
    # Measured ~17 ms on the dev machine; the generous bound keeps this stable on slow CI.
    assert best < 0.150


def test_none_is_identity_even_for_stereo():
    stereo = np.zeros((RATE, 2), dtype=np.int16)
    assert apply_voice_fx(stereo, RATE, "none") is stereo
