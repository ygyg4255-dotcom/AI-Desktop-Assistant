"""Optional Groq Whisper speech-to-text (``whisper-large-v3-turbo``).

Only used for audio transcription when ``GROQ_API_KEY`` is configured; the chat
model path stays Anthropic-only (audit S2). The endpoint is a hard-coded
constant, never read from configuration, so a config change cannot redirect
the key to another host. Callers fall back to local faster-whisper on any
error raised here.
"""
import io
import json
import time
import urllib.error
import urllib.request
import uuid
import wave
from typing import Optional

from src.config.log import get_logger, redact_secrets

logger = get_logger(__name__)

GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = "whisper-large-v3-turbo"
GROQ_TIMEOUT_SECONDS = 5.0
# After a failure, skip Groq for a while so an offline machine does not pay the
# network timeout on every utterance.
GROQ_COOLDOWN_SECONDS = 30.0


class GroqSTTError(RuntimeError):
    """Raised for any Groq transcription failure (network, HTTP, bad payload)."""


def pcm16_to_wav(pcm: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw mono int16 PCM in an in-memory WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def _multipart(fields: dict, file_name: str, file_bytes: bytes, content_type: str):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
         f"Content-Type: {content_type}\r\n\r\n").encode("utf-8")
        + file_bytes + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def transcribe_wav(wav_bytes: bytes, api_key: str, timeout: float = GROQ_TIMEOUT_SECONDS) -> str:
    """Send a WAV recording to Groq Whisper and return the transcript.

    Raises ``GroqSTTError`` on any failure; the message never contains the key.
    """
    if not api_key:
        raise GroqSTTError("GROQ_API_KEY is not configured")
    body, content_type = _multipart(
        {"model": GROQ_STT_MODEL, "response_format": "json", "temperature": "0"},
        "speech.wav", wav_bytes, "audio/wav",
    )
    req = urllib.request.Request(
        GROQ_TRANSCRIPTION_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": content_type,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raise GroqSTTError(redact_secrets(f"Groq HTTP {err.code}", api_key)) from None
    except Exception as err:  # URLError, timeout, bad JSON, ...
        raise GroqSTTError(redact_secrets(f"Groq request failed: {type(err).__name__}: {err}", api_key)) from None
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise GroqSTTError("Groq response had no 'text' field")
    return text.strip()


class GroqTranscriber:
    """Groq transcription with a failure cooldown. ``transcribe`` returns None to mean 'use local'."""

    def __init__(self, api_key: Optional[str], timeout: float = GROQ_TIMEOUT_SECONDS,
                 cooldown: float = GROQ_COOLDOWN_SECONDS):
        self._api_key = (api_key or "").strip()
        self._timeout = timeout
        self._cooldown = cooldown
        self._skip_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> Optional[str]:
        if not self._api_key or time.monotonic() < self._skip_until:
            return None
        try:
            return transcribe_wav(pcm16_to_wav(pcm, sample_rate), self._api_key, self._timeout)
        except GroqSTTError as err:
            self._skip_until = time.monotonic() + self._cooldown
            logger.warning("Groq STT failed, using local faster-whisper: %s", redact_secrets(err, self._api_key))
            return None
