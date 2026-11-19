"""Queued, non-blocking TTS: ordering, back-to-back playback, stop(), engine fallback."""
import threading
import time

import pytest
from PySide6.QtCore import Qt

from src.services import voice_service
from src.services.voice_service import TTSManager, is_speaking_event


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(voice_service, "update_hud_status", lambda *a, **k: None)
    is_speaking_event.clear()
    yield
    is_speaking_event.clear()


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def tts():
    m = TTSManager(enabled=True, warm_up=False)  # no mixer init in tests
    m._synthesize = lambda text: None  # never hit the network in tests
    m.finished = []
    m.started = []
    m.speech_finished.connect(lambda: m.finished.append(1), Qt.DirectConnection)
    m.speech_started.connect(lambda t: m.started.append(t), Qt.DirectConnection)
    return m


def test_enqueue_returns_immediately_and_sentences_play_in_order(tts):
    spoken, gate_during = [], []
    release = threading.Event()

    def engine(text):
        gate_during.append(is_speaking_event.is_set())
        spoken.append(text)
        release.wait(2)

    tts._speak_one = engine
    t0 = time.perf_counter()
    for s in ("First sentence.", "Second sentence.", "Third sentence."):
        tts.enqueue(s)
    assert time.perf_counter() - t0 < 0.5          # never blocks on playback
    assert wait_for(lambda: spoken == ["First sentence."])
    release.set()
    assert wait_for(lambda: len(tts.finished) >= 1 and len(spoken) == 3)
    assert spoken == ["First sentence.", "Second sentence.", "Third sentence."]
    assert all(gate_during)                         # mic gate held for every sentence
    assert wait_for(lambda: not is_speaking_event.is_set())
    assert tts.finished == [1]                      # speech_finished only once the queue drained
    assert tts.started == spoken


def test_single_dedicated_worker_thread(tts):
    names = set()
    tts._speak_one = lambda text: names.add(threading.current_thread().name)
    for i in range(4):
        tts.enqueue(f"Sentence number {i}.")
    assert wait_for(lambda: tts.finished)
    assert names == {"AltronTTSWorker"}


def test_stop_discards_pending_and_interrupts_current(tts):
    spoken = []
    started = threading.Event()
    seen_stop = []

    def engine(text):
        spoken.append(text)
        started.set()
        seen_stop.append(wait_for(lambda: tts._stop_requested, timeout=2))

    tts._speak_one = engine
    tts.enqueue("Playing now.")
    tts.enqueue("Never heard one.")
    tts.enqueue("Never heard two.")
    assert started.wait(2)
    tts.stop()
    assert wait_for(lambda: tts.finished)
    assert spoken == ["Playing now."] and seen_stop == [True]
    assert not is_speaking_event.is_set()


def test_speak_interrupts_then_plays_new_text(tts):
    spoken = []
    started = threading.Event()

    def engine(text):
        spoken.append(text)
        started.set()
        if text.startswith("Old"):
            wait_for(lambda: tts._stop_requested, timeout=2)

    tts._speak_one = engine
    tts.enqueue("Old sentence.")
    assert started.wait(2)
    tts.speak("Brand new.")
    assert wait_for(lambda: "Brand new." in spoken)
    assert spoken == ["Old sentence.", "Brand new."]


def test_disabled_or_empty_text_is_not_queued_but_finishes(tts):
    tts._speak_one = lambda text: pytest.fail("must not speak")
    tts.is_muted = True
    tts.enqueue("Muted.")
    tts.is_muted = False
    tts.enqueue("   ")
    assert len(tts.finished) == 2 and tts._queue.empty()


def test_engine_order_piper_then_edge_then_system(tts, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, "_speak_piper", lambda t: calls.append("piper") or False)
    monkeypatch.setattr(tts, "_speak_edge_tts", lambda t: calls.append("edge") or False)
    monkeypatch.setattr(tts, "_speak_system_fallbacks", lambda t: calls.append("system"))
    tts._speak_one("Hello there.")
    assert calls == ["piper", "edge", "system"]


def test_piper_success_short_circuits_other_engines(tts, monkeypatch):
    monkeypatch.setattr(tts, "_speak_piper", lambda t: True)
    monkeypatch.setattr(tts, "_speak_edge_tts", lambda t: pytest.fail("edge must not run"))
    monkeypatch.setattr(tts, "_speak_system_fallbacks", lambda t: pytest.fail("system must not run"))
    tts._speak_one("Hello there.")


def test_piper_not_configured_falls_through_without_error(tts, monkeypatch):
    monkeypatch.setattr(voice_service, "get_config_value", lambda *a, **k: "")
    assert tts._speak_piper("Hello.") is False


def test_piper_python_voice_is_used_and_played(tts, monkeypatch, tmp_path):
    model = tmp_path / "v.onnx"
    model.write_bytes(b"x")
    monkeypatch.setattr(voice_service, "get_config_value", lambda *a, **k: str(model))

    class Voice:
        class config:
            sample_rate = 16000

        def synthesize_stream_raw(self, text):
            yield b"\x01\x00" * 4

    played = {}

    class FakeSD:
        @staticmethod
        def play(samples, samplerate):
            played.update(n=len(samples), rate=samplerate)

        @staticmethod
        def wait():
            pass

    monkeypatch.setattr(tts, "_load_piper_voice", lambda p: Voice())
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", FakeSD)
    assert tts._speak_piper("Hello.") is True
    assert played == {"n": 4, "rate": 16000}


# -- sentence splitting + pipelining ---------------------------------------
def test_split_sentences_english_and_arabic():
    from src.services.voice_service import split_sentences
    assert split_sentences("Hello there. How are you? Fine! Costs 3.5 dollars.") == [
        "Hello there.", "How are you?", "Fine!", "Costs 3.5 dollars."]
    assert split_sentences("مرحبا بك. كيف حالك؟ بخير!") == ["مرحبا بك.", "كيف حالك؟", "بخير!"]
    assert split_sentences("no punctuation") == ["no punctuation"]


def test_multi_sentence_speak_is_split_and_ordered(tts):
    spoken = []
    tts._speak_one = lambda text: spoken.append(text)
    tts.speak("One here. Two here. Three here.")
    assert wait_for(lambda: len(spoken) == 3)
    assert spoken == ["One here.", "Two here.", "Three here."]


def test_next_sentence_is_prefetched_while_current_plays(tts, monkeypatch):
    monkeypatch.setattr(tts, "_piper_model_path", lambda: "")
    synthesized, release = [], threading.Event()
    tts._synthesize = lambda text: synthesized.append(text) or ("dry", text.encode())
    playing = threading.Event()

    def play(prepared):
        playing.set()
        release.wait(2)
        return True

    monkeypatch.setattr(tts, "_play_prepared", play)
    monkeypatch.setattr(tts, "_speak_piper", lambda t: False)
    tts.enqueue("First one. Second one. Third one.")
    assert playing.wait(2)
    # while sentence 1 is still "playing", the rest are already synthesized
    assert wait_for(lambda: synthesized == ["First one.", "Second one.", "Third one."])
    release.set()
    assert wait_for(lambda: tts.finished)
    assert tts.started == ["First one.", "Second one.", "Third one."]


def test_stop_cancels_prefetched_sentences_midqueue(tts, monkeypatch):
    monkeypatch.setattr(tts, "_piper_model_path", lambda: "")
    monkeypatch.setattr(tts, "_speak_piper", lambda t: False)
    gate, played = threading.Event(), []
    tts._synthesize = lambda text: gate.wait(2) and ("dry", b"x")

    def play(prepared):
        played.append(prepared)
        return True

    monkeypatch.setattr(tts, "_play_prepared", play)
    tts.enqueue("Alpha one. Beta two. Gamma three.")
    assert wait_for(lambda: tts.started == ["Alpha one."])
    t0 = time.perf_counter()
    tts.stop()
    gate.set()
    assert time.perf_counter() - t0 < 0.5
    assert wait_for(lambda: tts.finished)
    time.sleep(0.2)
    assert played == [] and tts.started == ["Alpha one."]
    assert tts._queue.empty() and tts._prefetch_q.empty()
    assert not is_speaking_event.is_set()


# -- startup-latency work ----------------------------------------------------
def test_split_first_clause_splits_long_first_sentence_after_60_chars():
    from src.services.voice_service import split_first_clause
    s = "x" * 65 + ", and then the rest of the sentence continues here."
    head, tail = split_first_clause(s)
    assert head.endswith(",") and len(head) == 66
    assert tail.startswith("and then")
    assert split_first_clause("Short, sentence.") == ("Short, sentence.", "")
    head, tail = split_first_clause("ا" * 61 + "، والباقي")
    assert head.endswith("،") and tail == "والباقي"


def test_only_first_chunk_of_utterance_is_split(tts):
    long_first = "w" * 70 + ", tail part one. Second sentence, with a comma that is short."
    tts._synthesize = lambda text: None
    tts.enqueue(long_first)
    assert wait_for(lambda: len(tts.started) >= 3)
    assert tts.started[0].endswith(",")
    assert tts.started[-1] == "Second sentence, with a comma that is short."


def test_edge_synth_reuses_one_persistent_event_loop(tts, monkeypatch):
    import asyncio
    import sys
    import types

    class FakeComm:
        def __init__(self, *a, **k):
            pass

        async def stream(self):
            yield {"type": "audio", "data": b"mp3"}

    fake_edge = types.SimpleNamespace(Communicate=FakeComm)
    fake_pg = types.SimpleNamespace(mixer=types.SimpleNamespace(get_init=lambda: True))
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)
    monkeypatch.setitem(sys.modules, "pygame", fake_pg)
    monkeypatch.setattr("src.services.voice_service.get_config_value",
                        lambda *a, **k: "none")
    assert tts._synthesize_edge("one") == ("dry", b"mp3")
    loop = tts._loop
    assert isinstance(loop, asyncio.AbstractEventLoop) and not loop.is_closed()
    assert tts._synthesize_edge("two") == ("dry", b"mp3")
    assert tts._loop is loop
    assert {"edge_tts_synth_ms", "decode_and_fx_ms"} <= set(tts._synth_timing)
