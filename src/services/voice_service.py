"""Voice services for AI Desktop Assistant: Text-to-Speech and Speech-to-Text."""
import re
import queue
import threading
import subprocess
import time
from typing import Optional, List

from PySide6.QtCore import QObject, QThread, Signal


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
    fresh SAPI.SpVoice / pyttsx3 / PowerShell fallbacks, guaranteeing that
    speech_finished always fires across all sequential tasks.
    """

    speech_started = Signal(str)
    speech_finished = Signal()

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

    def _speak_worker(self, text: str) -> None:
        """Per-utterance worker running in a fresh thread with COM isolation."""
        try:
            self.speech_started.emit(text)
        except Exception:
            pass

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

                # 1. Fresh SAPI.SpVoice (most reliable & non-leaking on Windows COM)
                try:
                    import win32com.client
                    speaker = win32com.client.Dispatch("SAPI.SpVoice")
                    speaker.Rate = 1
                    with self._lock:
                        self._active_sapi_voice = speaker
                    # 0 = Synchronous speak
                    speaker.Speak(text, 0)
                    spoken = True
                except Exception:
                    spoken = False
                finally:
                    with self._lock:
                        self._active_sapi_voice = None

                # 2. Fresh pyttsx3 fallback if SAPI failed
                if not spoken and not self._stop_requested:
                    try:
                        import pyttsx3
                        engine = pyttsx3.init()
                        engine.setProperty("rate", 185)
                        engine.say(text)
                        engine.runAndWait()
                        engine.stop()
                        spoken = True
                    except Exception:
                        spoken = False

                # 3. Subprocess PowerShell System.Speech fallback
                if not spoken and not self._stop_requested:
                    try:
                        ps_code = (
                            "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
                            "Add-Type -AssemblyName System.Speech; "
                            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                            "$synth.Rate = 1; "
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
            # 100% guarantee that speech_finished is emitted so subsequent tasks never deadlock
            try:
                self.speech_finished.emit()
            except Exception:
                pass


class SpeechRecognitionWorker(QThread):
    """Always-On Continuous Speech Recognition background worker."""

    started_listening = Signal()
    status_updated = Signal(str)
    text_recognized = Signal(str)
    error_occurred = Signal(str)
    finished_listening = Signal()

    def __init__(self, languages: Optional[List[str]] = None, parent=None):
        super().__init__(parent)
        self._is_aborted = False
        self._is_paused = False
        # Support English and Arabic (both Modern Standard and regional dialects)
        self._languages = languages or ["en-US", "ar-SA", "ar-MA"]

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
        """Continuously listen for speech in an always-on background loop."""
        self._is_aborted = False
        try:
            import speech_recognition as sr
        except ImportError:
            self.error_occurred.emit("Speech recognition library not installed.")
            self.finished_listening.emit()
            return

        recognizer = sr.Recognizer()
        recognizer.energy_threshold = 300
        recognizer.dynamic_energy_threshold = True

        try:
            with sr.Microphone() as source:
                self.started_listening.emit()
                self.status_updated.emit("Calibrating ambient noise...")
                recognizer.adjust_for_ambient_noise(source, duration=0.6)

                while not self._is_aborted:
                    # Echo & execution prevention: while paused, sleep and discard
                    if self._is_paused:
                        time.sleep(0.08)
                        continue

                    self.status_updated.emit("Listening...")

                    # Capture short phrase with responsive timeouts
                    try:
                        audio = recognizer.listen(source, timeout=3.0, phrase_time_limit=6.0)
                    except sr.WaitTimeoutError:
                        # Silence / timeout occurred: silently loop and keep listening
                        continue
                    except Exception:
                        if self._is_aborted:
                            break
                        time.sleep(0.15)
                        continue

                    # If aborted or paused while capturing, discard captured audio immediately
                    if self._is_aborted or self._is_paused:
                        continue

                    self.status_updated.emit("Transcribing...")

                    recognized_text = None
                    last_err = None

                    for lang in self._languages:
                        if self._is_aborted or self._is_paused:
                            break
                        try:
                            candidate = recognizer.recognize_google(audio, language=lang)
                            if candidate and candidate.strip():
                                recognized_text = candidate.strip()
                                break
                        except sr.UnknownValueError:
                            # Could not understand: try next language or silently discard
                            continue
                        except sr.RequestError as e:
                            last_err = f"Recognition network error: {e}"
                            break
                        except Exception as e:
                            last_err = str(e)
                            continue

                    # Discard if paused arrived while transcribing
                    if self._is_aborted or self._is_paused:
                        continue

                    if recognized_text:
                        self.text_recognized.emit(recognized_text)
                    elif last_err:
                        self.error_occurred.emit(last_err)

        except Exception as e:
            if not self._is_aborted:
                self.error_occurred.emit(f"Microphone error: {e}")
        finally:
            self.finished_listening.emit()
