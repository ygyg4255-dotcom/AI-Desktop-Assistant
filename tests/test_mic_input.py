"""Mic-only input: loopback filtering, post-TTS echo cooldown, low-energy rejection."""
import numpy as np
import pytest

from src.config import settings
from src.services.audio_input import (
    EchoGate, POST_TTS_COOLDOWN_SECONDS, active_speech_rms, is_loopback_device,
    is_too_quiet, pick_input_device,
)
from src.services.voice_service import SpeechRecognitionListener


def dev(index, name, ch=2):
    return {"index": index, "name": name, "max_input_channels": ch}


DEVICES = [
    dev(0, "Microsoft Sound Mapper - Input"),
    dev(1, "Stereo Mix (Realtek Audio)"),
    dev(2, "Microphone (Realtek Audio)", 1),
    dev(3, "Speakers (Realtek Audio)", 0),
    dev(4, "Headset Microphone (USB)", 1),
]


@pytest.mark.parametrize("name", [
    "Stereo Mix (Realtek)", "Speakers [Loopback]", "What U Hear", "Wave Out Mix", "Monitor of Built-in Audio",
])
def test_loopback_names_are_detected(name):
    assert is_loopback_device(name)


def test_real_microphones_are_not_loopback():
    assert not is_loopback_device("Microphone (Realtek Audio)")


def test_default_loopback_is_skipped_for_a_real_mic():
    assert pick_input_device(DEVICES, default_index=1) == 2  # default is Stereo Mix


def test_real_default_is_kept():
    assert pick_input_device(DEVICES, default_index=4) == 4


def test_only_loopback_devices_means_no_choice():
    assert pick_input_device([dev(1, "Stereo Mix"), dev(2, "Loopback")], default_index=1) is None


def test_output_only_devices_are_never_chosen():
    assert pick_input_device([dev(3, "Speakers", 0)]) is None


@pytest.mark.parametrize("wanted,expected", [("usb", 4), ("4", 4), ("HEADSET", 4)])
def test_mic_override_by_name_or_index(wanted, expected):
    assert pick_input_device(DEVICES, preferred=wanted, default_index=2) == expected


def test_unmatched_override_falls_back_to_auto():
    assert pick_input_device(DEVICES, preferred="nonexistent", default_index=1) == 2


def test_mic_override_setting_is_read_from_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "resolve_env_path", lambda data_dir=None: tmp_path / ".env")
    (tmp_path / ".env").write_text("ALTRON_MIC_DEVICE=USB\n", encoding="utf-8")
    settings.invalidate_config_cache()
    try:
        assert settings.Settings().altron_mic_device == "USB"
    finally:
        settings.invalidate_config_cache()


# --- echo gate ----------------------------------------------------------------

class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def test_gate_discards_while_speaking_then_for_cooldown_after():
    clock = Clock()
    gate = EchoGate(clock=clock)
    assert gate.should_discard(False) is False         # idle: audio passes
    assert gate.should_discard(True) is True           # TTS playing
    clock.t += 5
    assert gate.should_discard(False) is True          # TTS just ended: still cooling down
    clock.t += POST_TTS_COOLDOWN_SECONDS - 0.01
    assert gate.should_discard(False) is True
    clock.t += 0.02
    assert gate.should_discard(False) is False         # cooldown over


def test_cooldown_is_about_point_eight_seconds():
    assert POST_TTS_COOLDOWN_SECONDS == pytest.approx(0.8)


def test_each_speech_restarts_the_cooldown():
    clock = Clock()
    gate = EchoGate(clock=clock)
    gate.should_discard(True)
    clock.t += 1
    assert gate.should_discard(False) is True
    clock.t += 0.5
    gate.should_discard(True)                          # speaks again mid-cooldown
    clock.t += 0.5
    assert gate.should_discard(False) is True          # new full cooldown, not the old one


# --- energy floor -------------------------------------------------------------

def pcm(amplitude, seconds=1.0, seed=0):
    n = int(16000 * seconds)
    noise = np.random.default_rng(seed).normal(0, amplitude, n)
    return np.clip(noise * 32768, -32768, 32767).astype(np.int16).tobytes()


def test_quiet_background_noise_is_rejected():
    assert is_too_quiet(pcm(0.002))
    assert is_too_quiet(b"\x00\x00" * 16000)


def test_normal_speech_level_passes():
    assert not is_too_quiet(pcm(0.05))


def test_silence_padding_does_not_hide_real_speech():
    padded = b"\x00\x00" * 16000 + pcm(0.05, 0.5) + b"\x00\x00" * 8000
    assert active_speech_rms(padded) >= 0.04
    assert not is_too_quiet(padded)


def test_listener_exposes_the_energy_check():
    assert SpeechRecognitionListener._is_too_quiet(pcm(0.001)) is True
    assert SpeechRecognitionListener._is_too_quiet(pcm(0.08)) is False
