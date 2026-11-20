"""Alarm chime generator, desktop notification helper, and the reminder fire path.

Nothing here touches a real sound card, a real toast or the network: pygame and PowerShell
are replaced with fakes, and the handler gets injected chime/notify/TTS doubles.
"""
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import main as main_mod
from src.services import alarm_chime as ac
from src.services import desktop_notify as dn
from src.services.reminder_service import Reminder


# --- chime generator ------------------------------------------------------------------

def test_chime_is_stereo_int16_with_expected_length():
    pcm = ac.build_chime(sample_rate=22050, channels=2)
    assert pcm.dtype == np.int16
    assert pcm.ndim == 2 and pcm.shape[1] == 2
    assert np.array_equal(pcm[:, 0], pcm[:, 1])  # both channels identical
    expected = ac.chime_duration_seconds() * 22050
    assert abs(pcm.shape[0] - expected) <= 2


def test_chime_mono_shape_and_headroom_never_clips():
    pcm = ac.build_chime(sample_rate=44100, channels=1)
    assert pcm.ndim == 1
    peak = int(np.max(np.abs(pcm.astype(np.int32))))
    assert peak <= 32767
    assert abs(peak - ac.VOLUME * 32767) <= 2  # normalised to the configured volume


def test_chime_has_three_pulses_separated_by_silence():
    pcm = ac.build_chime(sample_rate=44100, channels=1).astype(np.float64)
    pulse = int(ac.PULSE_SECONDS * 44100)
    gap = int(ac.GAP_SECONDS * 44100)
    span = pulse + gap
    # Gaps are digital silence, and each of the three pulses carries real energy.
    for i in range(2):
        assert np.count_nonzero(pcm[i * span + pulse:(i + 1) * span]) == 0
    for i in range(3):
        assert np.sqrt(np.mean(pcm[i * span:i * span + pulse] ** 2)) > 1000


def test_chime_rises_in_pitch_across_each_pulse():
    pcm = ac.build_chime(sample_rate=44100, channels=1).astype(np.float64)
    first = pcm[: int(ac.PULSE_SECONDS * 44100)]
    half = len(first) // 2
    # Zero-crossing rate grows through a rising sweep, so the second half crosses more often.
    def crossings(x):
        return int(np.count_nonzero(np.diff(np.signbit(x))))
    assert crossings(first[half:]) > crossings(first[:half])


@pytest.mark.parametrize("kwargs", [{"sample_rate": 0}, {"channels": 3}])
def test_chime_rejects_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        ac.build_chime(**kwargs)


def test_chime_duration_helper_matches_pulse_count():
    assert ac.chime_duration_seconds(1) == pytest.approx(ac.PULSE_SECONDS)
    assert ac.chime_duration_seconds(3) == pytest.approx(3 * ac.PULSE_SECONDS + 2 * ac.GAP_SECONDS)


class _FakeSound:
    def __init__(self):
        self.played = 0

    def play(self):
        self.played += 1


def _fake_pygame(freq=22050, channels=2, initialised=True):
    sound = _FakeSound()
    made = []
    mixer = SimpleNamespace(
        get_init=lambda: (freq, -16, channels) if initialised else None,
        init=MagicMock(),
    )
    sndarray = SimpleNamespace(make_sound=lambda arr: (made.append(arr), sound)[1])
    module = SimpleNamespace(mixer=mixer, sndarray=sndarray)
    return module, sound, made


def test_play_alarm_chime_plays_mixer_matched_sound(monkeypatch):
    fake, sound, made = _fake_pygame(freq=22050, channels=2)
    monkeypatch.setitem(sys.modules, "pygame", fake)
    monkeypatch.setitem(sys.modules, "pygame.sndarray", fake.sndarray)
    assert ac.play_alarm_chime(wait=False) is True
    assert sound.played == 1
    assert made[0].shape[1] == 2  # built for the mixer's stereo output


def test_play_alarm_chime_returns_false_when_audio_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "pygame", None)  # import raises ImportError
    assert ac.play_alarm_chime(wait=False) is False


def test_play_alarm_chime_returns_false_when_mixer_fails(monkeypatch):
    fake, _sound, _made = _fake_pygame(initialised=False)
    fake.mixer.init = MagicMock(side_effect=RuntimeError("no audio device"))
    monkeypatch.setitem(sys.modules, "pygame", fake)
    monkeypatch.setitem(sys.modules, "pygame.sndarray", fake.sndarray)
    assert ac.play_alarm_chime(wait=False) is False


# --- desktop notification ---------------------------------------------------------------

class _Done:
    returncode = 0
    stderr = b""


def test_notification_passes_text_via_environment_not_arguments(monkeypatch):
    monkeypatch.setattr(dn.sys, "platform", "win32")
    monkeypatch.setattr(dn, "_powershell", lambda: "powershell.exe")
    seen = {}

    def fake_run(args, env, **kwargs):
        seen["args"], seen["env"] = args, env
        return _Done()

    monkeypatch.setattr(dn.subprocess, "run", fake_run)
    hostile = 'buy milk"; Remove-Item -Recurse C:\\ #'
    assert dn.show_desktop_notification("Altron reminder", hostile) is True
    assert all(hostile not in a for a in seen["args"])  # never interpolated into the command
    assert seen["env"]["ALTRON_TOAST_TEXT"] == hostile
    assert seen["env"]["ALTRON_TOAST_TITLE"] == "Altron reminder"
    assert "shell" not in seen  # no shell=True (S7)


def test_notification_clips_title_and_text_to_balloon_limits(monkeypatch):
    monkeypatch.setattr(dn.sys, "platform", "win32")
    monkeypatch.setattr(dn, "_powershell", lambda: "powershell.exe")
    seen = {}
    monkeypatch.setattr(dn.subprocess, "run", lambda args, env, **k: (seen.update(env=env), _Done())[1])
    dn.show_desktop_notification("T" * 200, "x" * 1000)
    assert len(seen["env"]["ALTRON_TOAST_TITLE"]) <= dn.TITLE_MAX
    assert len(seen["env"]["ALTRON_TOAST_TEXT"]) <= dn.TEXT_MAX


def test_notification_never_raises_and_reports_failure(monkeypatch):
    monkeypatch.setattr(dn.sys, "platform", "win32")
    monkeypatch.setattr(dn, "_powershell", lambda: "powershell.exe")

    def boom(*a, **k):
        raise OSError("powershell vanished")

    monkeypatch.setattr(dn.subprocess, "run", boom)
    assert dn.show_desktop_notification("Altron", "hi") is False


def test_notification_is_skipped_off_windows_and_without_powershell(monkeypatch):
    monkeypatch.setattr(dn.sys, "platform", "linux")
    assert dn.show_desktop_notification("Altron", "hi") is False
    monkeypatch.setattr(dn.sys, "platform", "win32")
    monkeypatch.setattr(dn, "_powershell", lambda: None)
    assert dn.show_desktop_notification("Altron", "hi") is False


# --- reminder fire handler: chime + toast + TTS + Telegram -------------------------------

@pytest.fixture
def hud(monkeypatch):
    log = []
    monkeypatch.setattr(main_mod, "update_hud_status", lambda **k: log.append(k))
    return log


def _rem(message="stretch"):
    return Reminder("a1", message, time.time(), time.time())


def test_alarm_runs_chime_before_speech_and_shows_toast(hud):
    order = []
    tts = MagicMock(is_enabled=True, is_muted=False)
    tts.speak.side_effect = lambda text: order.append(("speak", text))
    chime = MagicMock(side_effect=lambda: order.append(("chime",)))
    toast = MagicMock(side_effect=lambda *a: order.append(("toast", a)))
    main_mod.make_reminder_handler(tts, notify_telegram=MagicMock(), chime=chime,
                                   notify_desktop=toast, run_async=False)(_rem())
    assert ("chime",) in order and ("speak", "Reminder: stretch") in order
    assert order.index(("chime",)) < order.index(("speak", "Reminder: stretch"))
    toast.assert_called_once_with("Altron reminder", "stretch")


def test_alarm_handler_is_non_blocking_even_when_chime_is_slow(hud):
    release = threading.Event()
    spoken = threading.Event()
    tts = MagicMock(is_enabled=True, is_muted=False)
    tts.speak.side_effect = lambda text: spoken.set()
    chime = MagicMock(side_effect=lambda: release.wait(5))   # simulates a 2 s chime
    toast = MagicMock(side_effect=lambda *a: release.wait(5))  # simulates the toast's display time
    tg = MagicMock()
    fire = main_mod.make_reminder_handler(tts, notify_telegram=tg, chime=chime, notify_desktop=toast)

    started = time.monotonic()
    fire(_rem())
    assert time.monotonic() - started < 1.0       # the scheduler thread is not held up
    tg.assert_called_once()                       # Telegram and HUD are synchronous
    assert hud and hud[0]["log_entry"] == "Altron: Reminder: stretch"
    assert not spoken.is_set()                    # speech waits for the chime
    release.set()
    assert spoken.wait(5)
    chime.assert_called_once()


def test_muted_alarm_is_silent_but_still_toasts_and_logs(hud):
    tts = MagicMock(is_enabled=True, is_muted=True)
    chime, toast = MagicMock(), MagicMock()
    main_mod.make_reminder_handler(tts, notify_telegram=MagicMock(), chime=chime,
                                   notify_desktop=toast, run_async=False)(_rem())
    chime.assert_not_called()
    tts.speak.assert_not_called()
    toast.assert_called_once()
    assert "muted" in hud[0]["log_entry"]


def test_disabled_tts_is_treated_as_silenced(hud):
    tts = MagicMock(is_enabled=False, is_muted=False)
    chime = MagicMock()
    main_mod.make_reminder_handler(tts, notify_telegram=MagicMock(), chime=chime,
                                   notify_desktop=MagicMock(), run_async=False)(_rem())
    chime.assert_not_called()
    tts.speak.assert_not_called()


def test_failing_chime_still_speaks_and_toasts(hud):
    tts = MagicMock(is_enabled=True, is_muted=False)
    toast = MagicMock()
    main_mod.make_reminder_handler(tts, notify_telegram=MagicMock(),
                                   chime=MagicMock(side_effect=RuntimeError("no device")),
                                   notify_desktop=toast, run_async=False)(_rem())
    tts.speak.assert_called_once_with("Reminder: stretch")
    toast.assert_called_once()


def test_failing_toast_does_not_stop_chime_or_speech(hud):
    tts = MagicMock(is_enabled=True, is_muted=False)
    chime = MagicMock()
    main_mod.make_reminder_handler(tts, notify_telegram=MagicMock(), chime=chime,
                                   notify_desktop=MagicMock(side_effect=OSError("toast")),
                                   run_async=False)(_rem())
    chime.assert_called_once()
    tts.speak.assert_called_once_with("Reminder: stretch")


def test_missed_reminder_is_spoken_and_toasted_as_missed(hud):
    tts = MagicMock(is_enabled=True, is_muted=False)
    toast = MagicMock()
    late = Reminder("a2", "pills", time.time() - 3600, time.time() - 7200)
    main_mod.make_reminder_handler(tts, notify_telegram=MagicMock(), chime=MagicMock(),
                                   notify_desktop=toast, run_async=False)(late)
    assert tts.speak.call_args.args[0].startswith("Reminder (missed at ")
    assert toast.call_args.args[1] == "pills"
