"""Groq Whisper STT with local faster-whisper fallback, and gsk_ key redaction."""
import io
import json
import logging
import urllib.error
import wave

import numpy as np
import pytest

from src.config import settings
from src.config.log import RedactingFormatter, redact_secrets
from src.services import groq_stt
from src.services.groq_stt import GroqTranscriber, pcm16_to_wav, transcribe_wav
from src.services.voice_service import POST_SPEECH_SILENCE_FRAMES, SpeechRecognitionListener

FAKE_KEY = "gsk_" + "A1b2C3d4E5f6G7h8I9j0"
PCM = (np.zeros(1600, dtype=np.int16)).tobytes()


class _Resp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "resolve_env_path", lambda data_dir=None: tmp_path / ".env")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    settings.invalidate_config_cache()
    yield
    settings.invalidate_config_cache()


class _FakeWhisper:
    def __init__(self, text="local text"):
        self.calls = 0
        self._text = text

    def transcribe(self, samples, beam_size=1):
        self.calls += 1
        seg = type("Seg", (), {"text": self._text})()
        return [seg], None


def _listener(key=None, whisper=None):
    listener = SpeechRecognitionListener()
    listener._groq = GroqTranscriber(key)
    listener._whisper_model = whisper
    return listener


def test_wav_is_valid_16k_mono():
    with wave.open(io.BytesIO(pcm16_to_wav(PCM)), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.getnframes() == 1600


def test_groq_success_sends_expected_request_and_skips_local(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen.update(url=req.full_url, timeout=timeout, auth=req.get_header("Authorization"),
                    ctype=req.get_header("Content-type"), body=req.data)
        return _Resp({"text": "  hello altron "})

    monkeypatch.setattr(groq_stt.urllib.request, "urlopen", fake_urlopen)
    whisper = _FakeWhisper()
    listener = _listener(FAKE_KEY, whisper)

    assert listener._transcribe(PCM, np.zeros(1600, dtype=np.float32)) == "hello altron"
    assert whisper.calls == 0
    assert seen["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert seen["timeout"] <= 5.0
    assert seen["auth"] == f"Bearer {FAKE_KEY}"
    assert seen["ctype"].startswith("multipart/form-data; boundary=")
    assert b"whisper-large-v3-turbo" in seen["body"] and b"RIFF" in seen["body"]


@pytest.mark.parametrize("failure", [
    urllib.error.URLError("offline"),
    TimeoutError("slow"),
    urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO(b"")),
])
def test_groq_error_falls_back_to_faster_whisper(monkeypatch, failure):
    def boom(req, timeout=None):
        raise failure

    monkeypatch.setattr(groq_stt.urllib.request, "urlopen", boom)
    whisper = _FakeWhisper("local text")
    listener = _listener(FAKE_KEY, whisper)
    assert listener._transcribe(PCM, np.zeros(1600, dtype=np.float32)) == "local text"
    assert whisper.calls == 1


def test_groq_bad_payload_falls_back(monkeypatch):
    monkeypatch.setattr(groq_stt.urllib.request, "urlopen", lambda req, timeout=None: _Resp({"nope": 1}))
    whisper = _FakeWhisper()
    assert _listener(FAKE_KEY, whisper)._transcribe(PCM, np.zeros(10, dtype=np.float32)) == "local text"


def test_unset_key_never_touches_network(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("network must not be used without GROQ_API_KEY")

    monkeypatch.setattr(groq_stt.urllib.request, "urlopen", forbidden)
    whisper = _FakeWhisper()
    listener = _listener(None, whisper)
    assert listener._groq.enabled is False
    assert listener._transcribe(PCM, np.zeros(10, dtype=np.float32)) == "local text"


def test_failure_triggers_cooldown_so_offline_does_not_retry_every_utterance(monkeypatch):
    calls = []

    def boom(req, timeout=None):
        calls.append(1)
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(groq_stt.urllib.request, "urlopen", boom)
    tr = GroqTranscriber(FAKE_KEY)
    assert tr.transcribe(PCM) is None and tr.transcribe(PCM) is None
    assert len(calls) == 1


def test_error_message_does_not_leak_key(monkeypatch):
    def boom(req, timeout=None):
        raise RuntimeError(f"bad header Bearer {FAKE_KEY}")

    monkeypatch.setattr(groq_stt.urllib.request, "urlopen", boom)
    with pytest.raises(groq_stt.GroqSTTError) as exc:
        transcribe_wav(b"x", FAKE_KEY)
    assert FAKE_KEY not in str(exc.value)


def test_key_is_read_through_get_config_value(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("GROQ_API_KEY=gsk_fromfile12345\n", encoding="utf-8")
    settings.invalidate_config_cache()
    assert SpeechRecognitionListener()._groq.enabled is True
    assert settings.Settings().groq_api_key == "gsk_fromfile12345"


def test_vad_post_speech_silence_is_about_point_six_seconds():
    assert POST_SPEECH_SILENCE_FRAMES * 0.03 == pytest.approx(0.6, abs=0.001)


# --- gsk_ redaction (S6) -------------------------------------------------

def test_redact_secrets_strips_gsk_keys():
    out = redact_secrets(f"Authorization: Bearer {FAKE_KEY} failed")
    assert FAKE_KEY not in out and "gsk_<redacted>" in out


def test_gsk_redacted_from_log_message_and_traceback():
    fmt = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError(f"401 for {FAKE_KEY}")
    except RuntimeError:
        import sys
        rec = logging.LogRecord("altron.t", logging.ERROR, __file__, 1, f"key={FAKE_KEY}", None, sys.exc_info())
    assert FAKE_KEY not in fmt.format(rec)
