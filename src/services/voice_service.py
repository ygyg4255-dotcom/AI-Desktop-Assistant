"""Voice services for AI Desktop Assistant: Text-to-Speech and Speech-to-Text."""
import asyncio
import collections
import io
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

from src.services.signals import CallbackSignal

from src.services.hud_status import update_hud_status

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


class TTSManager(QObject):
    """Thread-safe Text-to-Speech manager.

    Spawns a fresh worker thread for every utterance with CoInitialize and
    fresh Piper TTS / SAPI.SpVoice / pyttsx3 / PowerShell fallbacks,
    guaranteeing that speech_finished always fires across all sequential tasks.
    """

    speech_started = Signal(str)
    speech_finished = Signal()
    is_speaking_event = is_speaking_event

    def __init__(self, enabled: bool = True, parent=None):
        super().__init__(parent)
        self._enabled = enabled
        self._muted = False
        self._lock = threading.Lock()
        self._stop_requested = False
        self._current_ps_proc: Optional[subprocess.Popen] = None
        self._active_sapi_voice = None

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

    def speak(self, text: str) -> None:
        """Enqueue/speak cleaned text on a fresh worker thread."""
        if not self._enabled or self._muted or not text:
            try:
                self.speech_finished.emit()
            except Exception:
                pass
            return

        cleaned = clean_text_for_speech(text)
        if not cleaned:
            try:
                self.speech_finished.emit()
            except Exception:
                pass
            return

        # Stop any active utterance before starting the new one
        self.stop()
        self._stop_requested = False

        thread = threading.Thread(
            target=self._speak_worker,
            args=(cleaned,),
            daemon=True
        )
        thread.start()

    def stop(self) -> None:
        """Immediately stop speaking and discard pending speech."""
        self._stop_requested = True
        is_speaking_event.clear()
        update_hud_status(state="Idle")
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass

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

    def _speak_edge_tts(self, text: str) -> bool:
        """Synthesize deep resonant male British butler/Altron voice using edge-tts."""
        try:
            import edge_tts
            import io
            import asyncio
            import pygame

            voice = "en-GB-RyanNeural"
            if any('؀' <= c <= 'ۿ' for c in text):
                voice = "ar-SA-HamedNeural"

            async def _generate():
                comm = edge_tts.Communicate(text, voice=voice, rate="+5%", pitch="-8Hz")
                chunks = []
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        chunks.append(chunk["data"])
                return b"".join(chunks)

            loop = asyncio.new_event_loop()
            try:
                mp3_data = loop.run_until_complete(_generate())
            finally:
                loop.close()

            if not mp3_data or self._stop_requested:
                return False

            if not pygame.mixer.get_init():
                pygame.mixer.init()

            pygame.mixer.music.load(io.BytesIO(mp3_data))
            pygame.mixer.music.play()

            while pygame.mixer.music.get_busy() and not self._stop_requested:
                time.sleep(0.04)

            if self._stop_requested:
                pygame.mixer.music.stop()
                return False

            return True
        except Exception:
            return False

    def _speak_piper(self, text: str) -> bool:
        """Attempt synthesis using Piper TTS if available."""
        piper_exe = shutil.which("piper") or shutil.which("piper.exe")
        if not piper_exe:
            return False
        model_path = os.getenv("PIPER_MODEL_PATH", "")
        if not model_path or not os.path.exists(model_path):
            return False
        try:
            proc = subprocess.Popen(
                [piper_exe, "--model", model_path, "--output-raw"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
            with self._lock:
                self._current_ps_proc = proc
            raw_audio, _ = proc.communicate(input=text.encode("utf-8"), timeout=15.0)
            if proc.returncode == 0 and raw_audio:
                try:
                    import sounddevice as sd
                    samples = np.frombuffer(raw_audio, dtype=np.int16)
                    sd.play(samples, samplerate=22050)
                    sd.wait()
                    return True
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            with self._lock:
                self._current_ps_proc = None
        return False

    def _speak_worker(self, text: str) -> None:
        """Per-utterance worker running in a fresh thread with COM isolation."""
        try:
            # Echo prevention: set gate so mic listener pauses before playback begins
            is_speaking_event.set()
            update_hud_status(state="Speaking")
            try:
                self.speech_started.emit(text)
            except Exception:
                pass

            # 1. Primary Fast Neural Male Voice (edge-tts: en-GB-RyanNeural)
            if not self._stop_requested:
                if self._speak_edge_tts(text):
                    return

            # 2. Piper TTS fallback if configured
            if not self._stop_requested:
                if self._speak_piper(text):
                    return

            co_init = False
            try:
                import pythoncom
                pythoncom.CoInitialize()
                co_init = True
            except Exception:
                try:
                    import comtypes
                    comtypes.CoInitialize()
                except Exception:
                    pass

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
            except Exception:
                pass
            finally:
                if co_init:
                    try:
                        import pythoncom
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass
        finally:
            # Echo prevention: clear gate immediately when playback finishes so listening resumes instantly
            is_speaking_event.clear()
            update_hud_status(state="Idle")
            # 100% guarantee that speech_finished is emitted so subsequent tasks never deadlock
            try:
                self.speech_finished.emit()
            except Exception:
                pass


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

        # Profiling durations
        self.last_listen_duration = 0.0
        self.last_stt_duration = 0.0

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
            stream = pa_instance.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                frames_per_buffer=480
            )
            stream_type = "pyaudio"
        except Exception as pa_err:
            try:
                import sounddevice as sd
                stream = sd.RawInputStream(
                    samplerate=16000,
                    blocksize=480,
                    channels=1,
                    dtype="int16"
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
        # Silence cutoff: 27 frames * 30ms = 0.81s (~0.8s cutoff)
        MAX_SILENCE_FRAMES = 27
        pre_roll_frames = collections.deque(maxlen=16)
        sliding_speech_flags = collections.deque(maxlen=16)
        audio_chunks = []
        is_recording = False
        silence_frame_count = 0
        t_record_start = 0.0

        try:
            while not self._is_aborted:
                # Echo prevention: if TTS is speaking or worker is paused, discard audio and sleep
                if is_speaking_event.is_set() or self._is_paused:
                    if is_recording:
                        audio_chunks.clear()
                        is_recording = False
                        silence_frame_count = 0
                    pre_roll_frames.clear()
                    sliding_speech_flags.clear()
                    time.sleep(0.04)
                    continue

                # Read 30ms frame (480 samples)
                try:
                    if stream_type == "pyaudio":
                        frame_bytes = stream.read(480, exception_on_overflow=False)
                    else:
                        frame_data, _ = stream.read(480)
                        frame_bytes = bytes(frame_data)
                except Exception:
                    time.sleep(0.02)
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

                    # Stop recording immediately after 0.8s of silence (or 15s max limit)
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
                        samples_int16 = np.frombuffer(raw_bytes, dtype=np.int16)
                        samples_float32 = samples_int16.astype(np.float32) / 32768.0

                        # Check if paused or echo arrived while buffering
                        if self._is_aborted or self._is_paused or is_speaking_event.is_set():
                            continue

                        self.status_updated.emit("Transcribing...")
                        t_stt_start = time.perf_counter()
                        recognized_text = ""

                        if self._whisper_model is not None:
                            try:
                                segments, _ = self._whisper_model.transcribe(
                                    samples_float32,
                                    beam_size=1
                                )
                                recognized_text = " ".join(s.text for s in segments).strip()
                            except Exception as stt_err:
                                print(f"[STT] faster-whisper error: {stt_err}")

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
