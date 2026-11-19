"""Voice services for AI Desktop Assistant: Text-to-Speech and Speech-to-Text."""
import asyncio
import collections
import io
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from typing import Optional, List

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal

from src.config.log import get_logger
from src.config.settings import get_config_value
from src.services.audio_input import (
    EchoGate, is_too_quiet, pick_input_device, MIN_UTTERANCE_RMS,
)
from src.services.groq_stt import GroqTranscriber
from src.services.sentence_chunker import SentenceChunker
from src.services.signals import CallbackSignal
from src.services.voice_fx import FX_NONE, FX_ULTRON, apply_voice_fx, normalize_fx_name

from src.services.hud_status import update_hud_status

logger = get_logger(__name__)

# Global echo prevention event: set when TTS is speaking, cleared when finished
is_speaking_event = threading.Event()


def clean_text_for_speech(text: str) -> str:
    """Prepare assistant response for pleasant, concise voice playback."""
    if not text:
        return ""

    # Remove markdown code blocks
    cleaned = re.sub(r"```[\s\S]*?```", " Code snippet omitted. ", text)
    # Remove inline code marks
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    # Remove markdown links [title](url) -> title
    cleaned = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", cleaned)
    # Remove markdown headers and emphasis
    cleaned = re.sub(r"#+\s*", "", cleaned)
    cleaned = re.sub(r"[*_~]", "", cleaned)
    # Remove bullet markers
    cleaned = re.sub(r"^\s*[-*+]\s+", "", cleaned, flags=re.MULTILINE)
    # Collapse multiple whitespaces/newlines
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Truncate if overly long for audio synthesis (up to ~350 chars)
    if len(cleaned) > 350:
        cut = cleaned[:350]
        last_period = cut.rfind(".")
        if last_period > 180:
            cleaned = cut[: last_period + 1]
        else:
            cleaned = cut.rstrip() + "..."

    return cleaned


WAKE_WORDS_PATTERN = re.compile(
    r'\b(?:altron|ultron|eltron|oltron|al\s+tron|el\s+tron|ul\s+tron|الترون|ألترون|إلترون|آلترون|أولترون|اولترون)\b',
    re.IGNORECASE
)


def parse_wake_word(text: str) -> tuple[bool, str]:
    """Check for wake word 'altron' (and phonetic variations) in transcribed speech.

    Returns:
        tuple[bool, str]: (has_wake_word, remaining_command)
        If wake word found, remaining_command contains everything after the wake word.
    """
    if not text:
        return False, ""
    match = WAKE_WORDS_PATTERN.search(text)
    if not match:
        return False, ""
    remainder = text[match.end():].lstrip(" ,.:;!?،؟\t\n")
    return True, remainder


class _AudioSlot:
    """Hand-off cell between the prefetch thread (fills ``audio``) and the player."""

    __slots__ = ("ready", "audio", "first", "t_enqueue", "timing")

    def __init__(self, first: bool = False) -> None:
        self.ready = threading.Event()
        self.audio = None
        self.first = first  # first sentence of an utterance: emit latency timings
        self.t_enqueue = time.monotonic()
        self.timing: dict = {}  # filled by the synthesizer (ms values)


FIRST_CLAUSE_MIN_CHARS = 60


def split_first_clause(sentence: str, min_chars: int = FIRST_CLAUSE_MIN_CHARS) -> tuple[str, str]:
    """Split a long first sentence at the first ``,``/``،`` found after ``min_chars``.

    Returns (head, tail); tail is "" when no split applies.
    """
    for i in range(min_chars, len(sentence)):
        if sentence[i] in ",،":
            head, tail = sentence[:i + 1].strip(), sentence[i + 1:].strip()
            if head and tail:
                return head, tail
            break
    return sentence, ""


def split_sentences(text: str) -> List[str]:
    """Split text into speakable sentences (English ``. ! ?``, Arabic ``؟ ،``, newlines)."""
    chunker = SentenceChunker()
    out = chunker.feed(text) + chunker.flush()
    return out or ([text.strip()] if text and text.strip() else [])


class TTSManager(QObject):
    """Thread-safe, non-blocking Text-to-Speech manager.

    Sentences go into a ``queue.Queue`` drained by one dedicated worker thread,
    so ``enqueue``/``speak`` return immediately and queued sentences play back
    to back. ``speak`` interrupts whatever is playing first; ``enqueue`` appends
    (used for streamed model text). Engines, in order: Piper (local, if
    installed and ``PIPER_MODEL_PATH`` is set), edge-tts, SAPI, pyttsx3,
    PowerShell System.Speech. The mic echo gate stays set while the queue has
    pending sentences; ``speech_finished`` fires once the queue drains.
    """

    speech_started = Signal(str)
    speech_finished = Signal()
    is_speaking_event = is_speaking_event

    def __init__(self, enabled: bool = True, parent=None, warm_up: bool = True):
        super().__init__(parent)
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # persistent, prefetch-thread-owned
        self._enabled = enabled
        self._muted = False
        self._lock = threading.Lock()
        self._current_ps_proc: Optional[subprocess.Popen] = None
        self._active_sapi_voice = None
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._prefetch_q: "queue.Queue[tuple]" = queue.Queue()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._current_slot: Optional[_AudioSlot] = None
        self._synthesize = self._synthesize_edge
        self._worker: Optional[threading.Thread] = None
        # stop() bumps _gen; each queued item remembers the generation it was
        # queued in, so anything older than the current generation is stale.
        self._gen = 0
        self._item_gen = 0
        self._piper_voice = None
        self._piper_failed = False
        self._piper_rate = 22050
        self._synth_timing: dict = {}
        if warm_up and enabled:
            threading.Thread(target=self._warm_up, name="AltronTTSWarmup", daemon=True).start()

    def _warm_up(self) -> None:
        """Init pygame.mixer once at startup so the first sentence doesn't pay for it."""
        try:
            import pygame
            if not pygame.mixer.get_init():
                pygame.mixer.init()
        except Exception as err:
            logger.debug("mixer warm-up failed (will retry lazily): %s", err)

    @property
    def _stop_requested(self) -> bool:
        """True when the utterance being spoken was interrupted by stop()."""
        return self._item_gen != self._gen

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @is_enabled.setter
    def is_enabled(self, val: bool) -> None:
        self._enabled = bool(val)
        if not self._enabled:
            self.stop()

    @property
    def is_muted(self) -> bool:
        return self._muted

    @is_muted.setter
    def is_muted(self, val: bool) -> None:
        self._muted = bool(val)
        if self._muted:
            self.stop()

    def _prepare(self, text: str) -> str:
        """Return speakable text, or "" after emitting speech_finished when nothing will play."""
        cleaned = clean_text_for_speech(text) if (self._enabled and not self._muted and text) else ""
        if not cleaned:
            try:
                self.speech_finished.emit()
            except Exception as err:
                logger.debug("speech_finished emit failed: %s", err)
        return cleaned

    def speak(self, text: str) -> None:
        """Interrupt anything playing, then speak ``text`` (non-blocking)."""
        cleaned = self._prepare(text)
        if not cleaned:
            return
        self.stop()
        self._put_all(cleaned)

    def enqueue(self, text: str) -> None:
        """Append ``text`` to the playback queue without interrupting (non-blocking)."""
        cleaned = self._prepare(text)
        if cleaned:
            self._put_all(cleaned)

    def _put_all(self, cleaned: str) -> None:
        """Split into sentences so the first one starts after one short synthesis."""
        sentences = split_sentences(cleaned)
        if sentences and self._queue.empty() and not is_speaking_event.is_set():
            # Utterance start: shorten the very first chunk so audio begins sooner.
            head, tail = split_first_clause(sentences[0])
            sentences[0:1] = [s for s in (head, tail) if s]
            first = True
        else:
            first = False
        for i, sentence in enumerate(sentences):
            self._put(sentence, first=first and i == 0)

    def _put(self, cleaned: str, first: bool = False) -> None:
        with self._lock:
            slot = _AudioSlot(first=first)
            self._queue.put((self._gen, cleaned, slot))
            self._prefetch_q.put((self._gen, cleaned, slot))
            if self._prefetch_thread is None or not self._prefetch_thread.is_alive():
                self._prefetch_thread = threading.Thread(
                    target=self._prefetch_loop, name="AltronTTSPrefetch", daemon=True)
                self._prefetch_thread.start()
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._worker_loop, name="AltronTTSWorker", daemon=True)
                self._worker.start()

    def stop(self) -> None:
        """Immediately stop speaking and discard pending speech."""
        with self._lock:
            self._gen += 1
            for q in (self._queue, self._prefetch_q):
                while True:
                    try:
                        _, _, slot = q.get_nowait()
                        slot.ready.set()  # unblock anything waiting on a dropped slot
                    except queue.Empty:
                        break
        is_speaking_event.clear()
        update_hud_status(state="Idle")
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
                pygame.mixer.stop()  # channels used by the FX playback path
        except Exception as err:
            logger.debug("pygame stop failed: %s", err)
        try:
            import sounddevice as sd
            sd.stop()
        except Exception as err:
            logger.debug("sounddevice stop failed: %s", err)

        with self._lock:
            if self._active_sapi_voice is not None:
                try:
                    # flag 2 = SVSFPurgeBeforeSpeak
                    self._active_sapi_voice.Speak("", 2)
                except Exception:
                    pass
                self._active_sapi_voice = None

            if self._current_ps_proc is not None:
                try:
                    self._current_ps_proc.terminate()
                except Exception:
                    pass
                self._current_ps_proc = None

    def _synthesize_edge(self, text: str):
        """edge-tts -> in-memory MP3 -> (optional) voice FX. Returns ("fx", Sound), ("dry", mp3) or None.

        Everything stays in RAM (no temp files, no subprocess). Safe to call from the
        prefetch thread so the next sentence is ready while the current one plays.
        """
        try:
            import edge_tts
            import pygame

            voice = "en-US-ChristopherNeural"
            if any('\u0600' <= c <= '\u06ff' for c in text):
                voice = "ar-SA-HamedNeural"

            async def _generate():
                # Deep, calm and deliberate: slight pitch drop and slower rate.
                comm = edge_tts.Communicate(text, voice=voice, rate="-5%", pitch="-10Hz")
                chunks = []
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        chunks.append(chunk["data"])
                return b"".join(chunks)

            t0 = time.monotonic()
            # One persistent loop (owned by the single prefetch thread) instead of
            # asyncio.new_event_loop()/close() per sentence.
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
            mp3_data = self._loop.run_until_complete(_generate())
            t1 = time.monotonic()
            self._synth_timing["edge_tts_synth_ms"] = (t1 - t0) * 1000
            if not mp3_data:
                return None

            if not pygame.mixer.get_init():
                pygame.mixer.init()
            fx = normalize_fx_name(get_config_value("ALTRON_VOICE_FX", default=FX_ULTRON))
            result = ("dry", mp3_data)
            if fx != FX_NONE:
                wet = self._apply_fx(pygame, mp3_data, fx)
                if wet is not None:
                    result = ("fx", wet)
            self._synth_timing["decode_and_fx_ms"] = (time.monotonic() - t1) * 1000
            return result
        except Exception as err:
            logger.debug("edge-tts synthesis failed, falling back: %s", err)
            return None

    def _apply_fx(self, pygame, mp3_data: bytes, fx: str):
        """Decode MP3 bytes in memory, apply the effect, return a pygame Sound (None on failure)."""
        try:
            import pygame.sndarray
            sound = pygame.mixer.Sound(file=io.BytesIO(mp3_data))
            rate = pygame.mixer.get_init()[0]
            dry = pygame.sndarray.array(sound)
            wet = apply_voice_fx(dry, rate, fx)
            if wet is dry:
                return None
            return pygame.sndarray.make_sound(np.ascontiguousarray(wet))
        except Exception as err:
            logger.debug("voice fx failed, using dry audio: %s", err)
            return None

    def _play_prepared(self, prepared) -> bool:
        """Play audio from ``_synthesize_edge``; polls ``_stop_requested`` so stop() is immediate."""
        import pygame
        kind, payload = prepared
        if self._stop_requested:
            return False
        slot = self._current_slot
        if slot is not None and slot.first:
            slot.timing["total_to_play_ms"] = (time.monotonic() - slot.t_enqueue) * 1000
            logger.info("TTS first-sentence latency: %s",
                        {k: round(v) for k, v in slot.timing.items()})
        if kind == "fx":
            channel = payload.play()
            if channel is None:
                return False
            while channel.get_busy() and not self._stop_requested:
                time.sleep(0.02)
            if self._stop_requested:
                channel.stop()
                return False
            return True
        pygame.mixer.music.load(io.BytesIO(payload))
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy() and not self._stop_requested:
            time.sleep(0.02)
        if self._stop_requested:
            pygame.mixer.music.stop()
            return False
        return True

    def _speak_edge_tts(self, text: str) -> bool:
        """Play the prefetched audio for this sentence (or synthesize now if none was queued)."""
        try:
            slot = self._current_slot
            if slot is not None:
                while not slot.ready.wait(0.03):
                    if self._stop_requested:
                        return False
                prepared = slot.audio
            else:
                prepared = self._synthesize_edge(text)
            if prepared is None or self._stop_requested:
                return False
            return self._play_prepared(prepared)
        except Exception as err:
            logger.debug("edge-tts playback failed, falling back: %s", err)
            return False

    def _prefetch_loop(self) -> None:
        """Synthesize queued sentences in order while earlier ones are still playing."""
        while True:
            gen, text, slot = self._prefetch_q.get()
            try:
                if gen == self._gen and not self._piper_model_path():
                    self._synth_timing = {}
                    slot.timing["enqueue_to_prefetch_start_ms"] = (time.monotonic() - slot.t_enqueue) * 1000
                    prepared = self._synthesize(text)
                    slot.timing.update(self._synth_timing)
                    if gen == self._gen:
                        slot.audio = prepared
            except Exception:
                logger.exception("TTS prefetch failed on one sentence")
            finally:
                slot.ready.set()

    # -- Piper (local, fast) ------------------------------------------------
    def _piper_model_path(self) -> str:
        path = get_config_value("PIPER_MODEL_PATH")
        return path if path and os.path.exists(path) else ""

    def _load_piper_voice(self, model_path: str):
        """Keep the Piper voice loaded between sentences (python ``piper-tts`` package)."""
        if self._piper_voice is None and not self._piper_failed:
            try:
                from piper import PiperVoice
                self._piper_voice = PiperVoice.load(model_path)
            except Exception as err:
                self._piper_failed = True
                logger.debug("piper python package unavailable, using CLI if present: %s", err)
        return self._piper_voice

    def _piper_synthesize_lib(self, voice, text: str):
        """Return (pcm_int16_bytes, sample_rate) across piper-tts 1.2 and 1.3+ APIs."""
        if hasattr(voice, "synthesize_stream_raw"):
            return b"".join(voice.synthesize_stream_raw(text)), int(voice.config.sample_rate)
        chunks = list(voice.synthesize(text))
        return b"".join(c.audio_int16_bytes for c in chunks), int(chunks[0].sample_rate)

    def _piper_synthesize_cli(self, model_path: str, text: str):
        piper_exe = shutil.which("piper") or shutil.which("piper.exe")
        if not piper_exe:
            return b"", self._piper_rate
        rate = self._piper_rate
        try:
            with open(model_path + ".json", encoding="utf-8") as fh:
                rate = int(json.load(fh).get("audio", {}).get("sample_rate", rate))
        except Exception as err:
            logger.debug("piper model config unreadable, assuming %s Hz: %s", rate, err)
        proc = subprocess.Popen(
            [piper_exe, "--model", model_path, "--output-raw"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        with self._lock:
            self._current_ps_proc = proc
        try:
            raw_audio, _ = proc.communicate(input=text.encode("utf-8"), timeout=15.0)
        finally:
            with self._lock:
                self._current_ps_proc = None
        return (raw_audio if proc.returncode == 0 else b""), rate

    def _speak_piper(self, text: str) -> bool:
        """Speak with Piper when installed and PIPER_MODEL_PATH points at a voice; else False."""
        model_path = self._piper_model_path()
        if not model_path:
            return False
        try:
            voice = self._load_piper_voice(model_path)
            if voice is not None:
                pcm, rate = self._piper_synthesize_lib(voice, text)
            else:
                pcm, rate = self._piper_synthesize_cli(model_path, text)
            if not pcm or self._stop_requested:
                return False
            import sounddevice as sd
            samples = np.frombuffer(pcm, dtype=np.int16)
            fx = normalize_fx_name(get_config_value("ALTRON_VOICE_FX", default=FX_ULTRON))
            if fx != FX_NONE:
                samples = apply_voice_fx(samples, rate, fx)  # same Ultron FX as edge-tts
            sd.play(samples, samplerate=rate)
            sd.wait()
            return not self._stop_requested
        except Exception as err:
            logger.debug("Piper TTS failed, falling back: %s", err)
            return False

    # -- queue worker -------------------------------------------------------
    def _worker_loop(self) -> None:
        """Drain the queue forever on one dedicated thread (sentences play back to back)."""
        while True:
            gen, text, slot = self._queue.get()
            try:
                if gen == self._gen:  # skip sentences queued before a stop()
                    self._item_gen = gen
                    self._current_slot = slot
                    # Echo prevention: mic listener pauses while anything is playing/pending
                    is_speaking_event.set()
                    update_hud_status(state="Speaking")
                    try:
                        self.speech_started.emit(text)
                    except Exception as err:
                        logger.debug("speech_started emit failed: %s", err)
                    self._speak_one(text)
            except Exception:
                logger.exception("TTS worker failed on one sentence")
            finally:
                if self._queue.empty():
                    is_speaking_event.clear()
                    update_hud_status(state="Idle")
                    # Always fires when the queue drains so later tasks never deadlock
                    try:
                        self.speech_finished.emit()
                    except Exception as err:
                        logger.debug("speech_finished emit failed: %s", err)

    def _speak_one(self, text: str) -> None:
        """Speak one sentence: Piper -> edge-tts -> system voices."""
        if not self._stop_requested and self._speak_piper(text):
            return
        if not self._stop_requested and self._speak_edge_tts(text):
            return
        if not self._stop_requested:
            self._speak_system_fallbacks(text)

    def _speak_system_fallbacks(self, text: str) -> None:
        """Windows SAPI, then pyttsx3, then PowerShell System.Speech (male voice enforced)."""
        co_init = False
        try:
            import pythoncom
            pythoncom.CoInitialize()
            co_init = True
        except Exception:
            try:
                import comtypes
                comtypes.CoInitialize()
            except Exception as err:
                logger.debug("TTS fallback step failed: %s", err)

        try:
            if not self._stop_requested:
                spoken = False

                # 3. Windows SAPI.SpVoice fallback with Male Voice Enforcement & Pitch Lowering
                try:
                    import win32com.client
                    speaker = win32com.client.Dispatch("SAPI.SpVoice")

                    voices = speaker.GetVoices()
                    male_voice = None
                    for i in range(voices.Count):
                        v = voices.Item(i)
                        desc = v.GetDescription().lower()
                        gender = v.GetAttribute("Gender").lower() if hasattr(v, "GetAttribute") else ""
                        # Strictly avoid female voices (Zira, Hazel)
                        if "zira" in desc or "hazel" in desc or gender == "female":
                            continue
                        if any(m in desc for m in ["david", "george", "mark", "james", "male"]) or gender == "male":
                            male_voice = v
                            break
                    if male_voice is not None:
                        speaker.Voice = male_voice

                    speaker.Rate = -1
                    with self._lock:
                        self._active_sapi_voice = speaker

                    escaped_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    sapi_xml = f"<pitch middle='-6'>{escaped_text}</pitch>"
                    try:
                        # 0 = Synchronous speak
                        speaker.Speak(sapi_xml, 0)
                    except Exception:
                        speaker.Speak(text, 0)
                    spoken = True
                except Exception:
                    spoken = False
                finally:
                    with self._lock:
                        self._active_sapi_voice = None

                # 4. Fresh pyttsx3 fallback with Male Voice Selection
                if not spoken and not self._stop_requested:
                    try:
                        import pyttsx3
                        engine = pyttsx3.init()
                        for v in engine.getProperty("voices"):
                            name_lower = v.name.lower()
                            if "zira" in name_lower or "hazel" in name_lower:
                                continue
                            if any(m in name_lower for m in ["david", "george", "mark", "james", "male"]):
                                engine.setProperty("voice", v.id)
                                break
                        engine.setProperty("rate", 160)
                        engine.say(text)
                        engine.runAndWait()
                        engine.stop()
                        spoken = True
                    except Exception:
                        spoken = False

                # 5. Subprocess PowerShell System.Speech fallback with Male Filter
                if not spoken and not self._stop_requested:
                    try:
                        ps_code = (
                            "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
                            "Add-Type -AssemblyName System.Speech; "
                            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                            "$male = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Gender -eq 'Male' -and $_.VoiceInfo.Name -notmatch 'Zira|Hazel' } | Select-Object -First 1; "
                            "if ($male) { $synth.SelectVoice($male.VoiceInfo.Name) }; "
                            "$synth.Rate = -1; "
                            "$t = [Console]::In.ReadToEnd(); "
                            "if ($t) { $synth.Speak($t) }"
                        )
                        proc = subprocess.Popen(
                            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_code],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            text=True,
                            encoding="utf-8"
                        )
                        with self._lock:
                            self._current_ps_proc = proc

                        proc.communicate(input=text, timeout=25.0)
                        with self._lock:
                            self._current_ps_proc = None
                    except Exception:
                        with self._lock:
                            self._current_ps_proc = None
        except Exception as err:
            logger.debug("TTS fallback step failed: %s", err)
        finally:
            if co_init:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception as err:
                    logger.debug("TTS fallback step failed: %s", err)


# Post-speech silence before an utterance is closed: 20 frames * 30ms = 0.6s.
POST_SPEECH_SILENCE_FRAMES = 20


class EnergyVAD:
    """Zero-dependency adaptive RMS energy-based Voice Activity Detector."""

    def __init__(self, sample_rate: int = 16000, initial_threshold: float = 0.012):
        self.sample_rate = sample_rate
        self.min_energy_threshold = initial_threshold
        self.ambient_energy = 0.003
        self.alpha = 0.95

    def is_speech(self, frame_bytes: bytes) -> bool:
        """Return True if 30ms 16kHz int16 mono audio frame contains speech."""
        try:
            samples = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(samples ** 2)))
        except Exception:
            return False

        # Dynamic threshold adapting to ambient background noise
        dynamic_thresh = max(self.min_energy_threshold, self.ambient_energy * 2.2)
        is_voice = (rms > dynamic_thresh)

        if not is_voice:
            self.ambient_energy = self.alpha * self.ambient_energy + (1.0 - self.alpha) * rms

        return is_voice


class FastVAD:
    """Fast VAD combining webrtcvad with zero-dependency EnergyVAD fallback."""

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self._webrtc = None
        try:
            import webrtcvad
            self._webrtc = webrtcvad.Vad(2)
        except Exception:
            self._webrtc = None
        self._energy_vad = EnergyVAD(sample_rate=sample_rate)

    def is_speech(self, frame_bytes: bytes) -> bool:
        if self._webrtc is not None:
            try:
                return bool(self._webrtc.is_speech(frame_bytes, self.sample_rate))
            except Exception:
                pass
        return self._energy_vad.is_speech(frame_bytes)


def _pyaudio_input_devices(pa):
    """(devices, default_index) for PyAudio, normalised for pick_input_device."""
    devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        devices.append({"index": i, "name": str(info.get("name", "")),
                        "max_input_channels": int(info.get("maxInputChannels", 0))})
    try:
        default = int(pa.get_default_input_device_info()["index"])
    except Exception as err:
        logger.debug("no default PyAudio input device: %s", err)
        default = None
    return devices, default


def _sounddevice_input_devices(sd):
    devices = [{"index": i, "name": str(d.get("name", "")),
                "max_input_channels": int(d.get("max_input_channels", 0))}
               for i, d in enumerate(sd.query_devices())]
    try:
        default = sd.default.device[0]
        default = int(default) if default is not None and int(default) >= 0 else None
    except Exception as err:
        logger.debug("no default sounddevice input: %s", err)
        default = None
    return devices, default


class SpeechRecognitionListener:
    """Always-on continuous speech recognition loop (VAD + fast CPU STT), Qt-free.

    The headless runtime (main.py) calls ``run()`` on a plain ``threading.Thread``;
    the signals are ``CallbackSignal`` objects invoked on that thread.
    """

    started_listening = CallbackSignal()
    status_updated = CallbackSignal()
    text_recognized = CallbackSignal()
    error_occurred = CallbackSignal()
    finished_listening = CallbackSignal()

    def __init__(self, languages: Optional[List[str]] = None, model_size: Optional[str] = None):
        self._is_aborted = False
        self._is_paused = False
        self._languages = languages or ["en-US", "ar-SA", "ar-MA"]

        # Fast CPU STT model configuration (downgrade large/medium to base for CPU performance)
        model = (model_size or os.getenv("WHISPER_MODEL", "base")).strip().lower()
        if model in ("large", "large-v1", "large-v2", "large-v3", "medium"):
            model = "base"
        self._model_size = model
        self._whisper_model = None
        # Optional fast cloud STT; local faster-whisper stays the fallback.
        self._groq = GroqTranscriber(get_config_value("GROQ_API_KEY"))

        # Profiling durations
        self.last_listen_duration = 0.0
        self.last_stt_duration = 0.0

    def _transcribe(self, raw_pcm: bytes, samples_float32) -> str:
        """Groq Whisper when configured (short timeout), else/on error local faster-whisper."""
        text = self._groq.transcribe(raw_pcm)
        if text is not None:
            return text
        if self._whisper_model is None:
            return ""
        try:
            segments, _ = self._whisper_model.transcribe(samples_float32, beam_size=1)
            return " ".join(s.text for s in segments).strip()
        except Exception as stt_err:
            logger.warning("faster-whisper error: %s", stt_err)
            return ""

    @staticmethod
    def _is_too_quiet(raw_pcm: bytes) -> bool:
        """True when the utterance is below the energy floor (would only feed Whisper hallucinations)."""
        return is_too_quiet(raw_pcm, MIN_UTTERANCE_RMS)

    def abort(self) -> None:
        """Request immediate abort and stop of the continuous listening thread."""
        self._is_aborted = True

    def pause(self) -> None:
        """Pause speech recognition to prevent echo during execution and TTS playback."""
        self._is_paused = True

    def resume(self) -> None:
        """Resume speech recognition after command execution and TTS complete."""
        self._is_paused = False

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    def run(self) -> None:
        """Continuously listen for speech using chunked VAD and fast CPU STT."""
        self._is_aborted = False

        # 1. Initialize faster-whisper STT if available
        try:
            from faster_whisper import WhisperModel
            self.status_updated.emit(f"Loading {self._model_size} STT model...")
            self._whisper_model = WhisperModel(
                self._model_size,
                device="cpu",
                compute_type="int8",
                cpu_threads=4
            )
            # Warm-up inference so first voice interaction has 0 cold-start latency
            warmup_pcm = np.zeros(1600, dtype=np.float32)
            list(self._whisper_model.transcribe(warmup_pcm, beam_size=1)[0])
        except Exception as e:
            print(f"[STT] faster-whisper unavailable ({e}), using standard fallback.")
            self._whisper_model = None

        # Fallback speech_recognition recognizer if needed
        sr_recognizer = None
        try:
            import speech_recognition as sr
            sr_recognizer = sr.Recognizer()
        except ImportError:
            pass

        # 2. Initialize VAD (30ms frames at 16kHz)
        vad = FastVAD(sample_rate=16000)

        # 3. Open audio input stream (PyAudio or sounddevice)
        stream_type = None
        pa_instance = None
        stream = None

        try:
            import pyaudio
            pa_instance = pyaudio.PyAudio()
            devices, default_idx = _pyaudio_input_devices(pa_instance)
            mic_index = pick_input_device(devices, get_config_value("ALTRON_MIC_DEVICE"), default_idx)
            logger.info("Microphone input device: %s", mic_index if mic_index is not None else "system default")
            open_kwargs = {"input_device_index": mic_index} if mic_index is not None else {}
            stream = pa_instance.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                frames_per_buffer=480,
                **open_kwargs
            )
            stream_type = "pyaudio"
        except Exception as pa_err:
            try:
                import sounddevice as sd
                devices, default_idx = _sounddevice_input_devices(sd)
                mic_index = pick_input_device(devices, get_config_value("ALTRON_MIC_DEVICE"), default_idx)
                logger.info("Microphone input device: %s", mic_index if mic_index is not None else "system default")
                stream = sd.RawInputStream(
                    samplerate=16000,
                    blocksize=480,
                    channels=1,
                    dtype="int16",
                    device=mic_index
                )
                stream.start()
                stream_type = "sounddevice"
            except Exception as sd_err:
                print(f"[Voice] Low-latency mic open failed: PyAudio ({pa_err}), sounddevice ({sd_err}).")

        # If native low-latency stream unavailable, run legacy fallback loop
        if stream_type is None:
            self._run_legacy_fallback(sr_recognizer)
            return

        self.started_listening.emit()
        self.status_updated.emit("Listening...")

        # 4. VAD Listening loop:
        # Frame size: 30ms = 480 samples = 960 bytes
        # Sliding window: 16 frames (~0.5s)
        # Silence cutoff: 20 frames * 30ms = 0.6s
        MAX_SILENCE_FRAMES = POST_SPEECH_SILENCE_FRAMES
        pre_roll_frames = collections.deque(maxlen=16)
        sliding_speech_flags = collections.deque(maxlen=16)
        audio_chunks = []
        is_recording = False
        silence_frame_count = 0
        t_record_start = 0.0
        echo_gate = EchoGate()

        try:
            while not self._is_aborted:
                # Read 30ms frame (480 samples). Frames are read even while gated so the
                # OS buffer never holds audio captured during TTS playback.
                try:
                    if stream_type == "pyaudio":
                        frame_bytes = stream.read(480, exception_on_overflow=False)
                    else:
                        frame_data, _ = stream.read(480)
                        frame_bytes = bytes(frame_data)
                except Exception as read_err:
                    logger.debug("mic read failed: %s", read_err)
                    time.sleep(0.02)
                    continue

                # Echo prevention: while TTS speaks (and for ~0.8s after) or when paused,
                # discard the frame and any partial recording.
                if echo_gate.should_discard(is_speaking_event.is_set() or self._is_paused):
                    if is_recording:
                        audio_chunks.clear()
                        is_recording = False
                        silence_frame_count = 0
                    pre_roll_frames.clear()
                    sliding_speech_flags.clear()
                    continue

                if not frame_bytes or len(frame_bytes) != 960:
                    continue

                is_voice = vad.is_speech(frame_bytes)
                pre_roll_frames.append(frame_bytes)
                sliding_speech_flags.append(1 if is_voice else 0)

                # State A: Not recording -> start ONLY when actual speech is detected
                if not is_recording:
                    if is_voice and sum(sliding_speech_flags) >= 2:
                        is_recording = True
                        update_hud_status(state="Listening")
                        audio_chunks = list(pre_roll_frames)
                        silence_frame_count = 0
                        t_record_start = time.perf_counter()
                        self.status_updated.emit("Listening (speech detected)...")

                # State B: Currently recording speech
                else:
                    audio_chunks.append(frame_bytes)
                    if is_voice:
                        silence_frame_count = 0
                    else:
                        silence_frame_count += 1

                    # Stop recording after 0.6s of silence (or 15s max limit)
                    duration_so_far = len(audio_chunks) * 0.03
                    if silence_frame_count >= MAX_SILENCE_FRAMES or duration_so_far >= 15.0:
                        t_record_end = time.perf_counter()
                        listen_vad_duration = t_record_end - t_record_start
                        is_recording = False
                        silence_frame_count = 0
                        pre_roll_frames.clear()
                        sliding_speech_flags.clear()

                        # Discard if too short (<0.4s) to be valid speech (e.g. click/pop)
                        if len(audio_chunks) < 14:
                            audio_chunks.clear()
                            continue

                        # Pass captured speech directly in-memory as NumPy float32 array (no disk writes)
                        raw_bytes = b"".join(audio_chunks)
                        audio_chunks.clear()
                        if self._is_too_quiet(raw_bytes):
                            logger.debug("Dropped low-energy audio (background noise)")
                            self.status_updated.emit("Listening...")
                            continue
                        samples_int16 = np.frombuffer(raw_bytes, dtype=np.int16)
                        samples_float32 = samples_int16.astype(np.float32) / 32768.0

                        # Check if paused or echo arrived while buffering
                        if self._is_aborted or self._is_paused or is_speaking_event.is_set():
                            continue

                        self.status_updated.emit("Transcribing...")
                        t_stt_start = time.perf_counter()
                        recognized_text = self._transcribe(raw_bytes, samples_float32)

                        # Safe fallback to speech_recognition if whisper failed or unavailable
                        if not recognized_text and sr_recognizer is not None:
                            try:
                                import speech_recognition as sr
                                audio_data = sr.AudioData(raw_bytes, 16000, 2)
                                for lang in self._languages:
                                    try:
                                        cand = sr_recognizer.recognize_google(audio_data, language=lang)
                                        if cand and cand.strip():
                                            recognized_text = cand.strip()
                                            break
                                    except Exception:
                                        pass
                            except Exception:
                                pass

                        t_stt_end = time.perf_counter()
                        stt_duration = t_stt_end - t_stt_start

                        self.last_listen_duration = listen_vad_duration
                        self.last_stt_duration = stt_duration

                        # Discard if echo arrived during STT
                        if self._is_aborted or self._is_paused or is_speaking_event.is_set():
                            continue

                        if recognized_text:
                            self.text_recognized.emit(recognized_text)
                        self.status_updated.emit("Listening...")

        except Exception as e:
            if not self._is_aborted:
                self.error_occurred.emit(f"Microphone error: {e}")
        finally:
            try:
                if stream_type == "pyaudio" and stream is not None:
                    stream.stop_stream()
                    stream.close()
                elif stream_type == "sounddevice" and stream is not None:
                    stream.stop()
                    stream.close()
            except Exception:
                pass
            if pa_instance is not None:
                try:
                    pa_instance.terminate()
                except Exception:
                    pass
            self.finished_listening.emit()

    def _run_legacy_fallback(self, recognizer) -> None:
        """Legacy SpeechRecognition fallback when low-latency stream is unavailable."""
        try:
            import speech_recognition as sr
            if recognizer is None:
                recognizer = sr.Recognizer()
            with sr.Microphone() as source:
                self.started_listening.emit()
                recognizer.adjust_for_ambient_noise(source, duration=0.6)
                while not self._is_aborted:
                    if self._is_paused or is_speaking_event.is_set():
                        time.sleep(0.08)
                        continue
                    try:
                        t0 = time.perf_counter()
                        audio = recognizer.listen(source, timeout=3.0, phrase_time_limit=6.0)
                        self.last_listen_duration = time.perf_counter() - t0
                    except sr.WaitTimeoutError:
                        continue
                    if self._is_aborted or self._is_paused or is_speaking_event.is_set():
                        continue
                    t_stt = time.perf_counter()
                    text = None
                    for lang in self._languages:
                        try:
                            cand = recognizer.recognize_google(audio, language=lang)
                            if cand and cand.strip():
                                text = cand.strip()
                                break
                        except Exception:
                            continue
                    self.last_stt_duration = time.perf_counter() - t_stt
                    if text and not (self._is_aborted or self._is_paused or is_speaking_event.is_set()):
                        self.text_recognized.emit(text)
        except Exception as e:
            if not self._is_aborted:
                self.error_occurred.emit(f"Microphone error: {e}")
        finally:
            self.finished_listening.emit()


class SpeechRecognitionWorker(QThread, SpeechRecognitionListener):
    """Legacy Qt wrapper (deprecated src/ui/ only): same loop, real Qt signals.

    Start it with ``start()``; never call ``run()`` on it from a Python thread.
    """

    started_listening = Signal()
    status_updated = Signal(str)
    text_recognized = Signal(str)
    error_occurred = Signal(str)
    finished_listening = Signal()

    def __init__(self, languages: Optional[List[str]] = None, model_size: Optional[str] = None, parent=None):
        QThread.__init__(self, parent)
        SpeechRecognitionListener.__init__(self, languages, model_size)

    def run(self) -> None:
        SpeechRecognitionListener.run(self)
