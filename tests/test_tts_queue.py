"""Queued, non-blocking TTS: ordering, back-to-back playback, stop(), engine fallback."""
import sys
import threading
import time

import pytest
from PySide6.QtCore import Qt

from src.services import voice_service
from src.services.voice_service import TTSManager, is_speaking_event


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    from src.services import piper_voices
    monkeypatch.setattr(voice_service, "update_hud_status", lambda *a, **k: None)
    # Never read real Piper voices from the user's data dir during tests.
    monkeypatch.setattr(piper_voices, "default_voices_dir", lambda: tmp_path / "no-voices")
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


def test_piper_not_configured_falls_through_without_error(tts, monkeypatch, tmp_path):
    from src.services import piper_voices
    monkeypatch.setattr(voice_service, "get_config_value", lambda *a, **k: "")
    monkeypatch.setattr(piper_voices, "default_voices_dir", lambda: tmp_path / "none")  # no real voices
    assert tts._speak_piper("Hello.") is False


def test_piper_python_voice_is_used_and_played(tts, monkeypatch, tmp_path):
    model = tmp_path / "v.onnx"
    model.write_bytes(b"x")
    monkeypatch.setattr(voice_service, "get_config_value", lambda *a, **k: str(model))

    class Voice:
        class config:
            sample_rate = 16000

        def synthesize_stream_raw(self, text, length_scale=None):
            assert length_scale == pytest.approx(1.06)
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
    monkeypatch.setattr(tts, "_piper_model_path", lambda *a, **k: "")
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
    monkeypatch.setattr(tts, "_piper_model_path", lambda *a, **k: "")
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
def test_split_first_clause_splits_long_first_sentence_after_30_chars():
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
    tts._speak_one = lambda text: None  # no real engines
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


def test_first_sentence_latency_stages_account_for_total(caplog):
    import logging
    from src.services.voice_service import _AudioSlot
    slot = _AudioSlot(first=True)
    slot.timing.update({"enqueue_to_prefetch_start_ms": 3.0, "import_setup_ms": 4000.0,
                        "edge_tts_synth_ms": 10.0, "decode_and_fx_ms": 5.0})
    slot.t_enqueue -= 4.5  # pretend 4.5 s elapsed
    slot.t_prefetch_done = time.monotonic() - 0.4
    with caplog.at_level(logging.INFO):
        TTSManager._log_first_latency(slot)
    t = slot.timing
    named = sum(t[k] for k in ("enqueue_to_prefetch_start_ms", "import_setup_ms", "edge_tts_synth_ms",
                               "decode_and_fx_ms", "handoff_to_play_ms"))
    assert t["total_to_play_ms"] == pytest.approx(named + t["other_ms"], abs=1.0)
    assert t["handoff_to_play_ms"] >= 400
    assert "TTS first-sentence latency" in caplog.text


# -- Piper: prefetch, fallback, per-language voices, warm-up ----------------
class FakeVoice:
    """Stands in for a piper-tts 1.2 voice; records (text, length_scale) per call."""

    class config:
        sample_rate = 16000

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def synthesize_stream_raw(self, text, length_scale=None):
        self.calls.append((text, length_scale))
        if self.fail:
            raise RuntimeError("piper exploded")
        yield b"\x01\x00" * 800


class FakeSoundDevice:
    def __init__(self):
        self.played = []

    def play(self, samples, samplerate):
        self.played.append((len(samples), samplerate))

    def wait(self):
        pass


def _cfg(values):
    def get(*keys, default=None, **_):
        for key in keys:
            if key in values:
                return values[key]
        return default
    return get


@pytest.fixture
def piper_env(tts, monkeypatch, tmp_path):
    """Isolate Piper: no real voices folder, a fake sounddevice, and a model file that exists."""
    from src.services import piper_voices
    monkeypatch.setattr(piper_voices, "default_voices_dir", lambda: tmp_path / "no-voices")
    en_model = tmp_path / "en.onnx"
    en_model.write_bytes(b"x")
    sd = FakeSoundDevice()
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    base = {"PIPER_MODEL_PATH": str(en_model), "ALTRON_VOICE_FX": "none"}
    return {"tts": tts, "mp": monkeypatch, "model": en_model, "sd": sd, "base": base, "tmp": tmp_path}


def test_piper_is_rendered_ahead_by_prefetch_and_rendered_once(piper_env):
    env, mp = piper_env, piper_env["mp"]
    tts = env["tts"]
    voice = FakeVoice()
    mp.setattr(voice_service, "get_config_value", _cfg(env["base"]))
    mp.setattr(tts, "_load_piper_voice", lambda p: voice)
    tts._synthesize = lambda text: pytest.fail("edge-tts must not run when Piper renders")
    tts.enqueue("Hello there. Second line.")
    assert wait_for(lambda: len(env["sd"].played) == 2)
    assert [c[0] for c in voice.calls] == ["Hello there.", "Second line."]
    assert all(rate == 16000 for _, rate in env["sd"].played)


def test_piper_failure_falls_back_to_prefetched_edge_audio(piper_env):
    env, mp = piper_env, piper_env["mp"]
    tts = env["tts"]
    mp.setattr(voice_service, "get_config_value", _cfg(env["base"]))
    mp.setattr(tts, "_load_piper_voice", lambda p: FakeVoice(fail=True))
    edge_calls = []
    tts._synthesize = lambda text: edge_calls.append(text) or ("dry", b"mp3")
    played = []
    mp.setattr(tts, "_play_prepared", lambda prepared: played.append(prepared) or True)
    tts.enqueue("Only one sentence here.")
    assert wait_for(lambda: tts.finished)
    assert edge_calls == ["Only one sentence here."]
    assert played == [("dry", b"mp3")]
    assert env["sd"].played == []


def test_arabic_text_never_uses_the_english_voice(piper_env):
    env, mp = piper_env, piper_env["mp"]
    tts = env["tts"]
    mp.setattr(voice_service, "get_config_value", _cfg(env["base"]))
    mp.setattr(tts, "_load_piper_voice", lambda p: pytest.fail("English voice must not read Arabic"))
    assert tts._speak_piper("مرحبا بك") is False


def test_warm_up_preloads_both_voices_with_probe_and_length_scale(piper_env, tmp_path):
    env, mp = piper_env, piper_env["mp"]
    tts = env["tts"]
    ar_model = tmp_path / "ar.onnx"
    ar_model.write_bytes(b"x")
    values = dict(env["base"], PIPER_MODEL_PATH_AR=str(ar_model))
    mp.setattr(voice_service, "get_config_value", _cfg(values))
    loaded = {}

    def load(path):
        loaded[path] = FakeVoice()
        return loaded[path]

    mp.setattr(tts, "_load_piper_voice", load)
    tts._preload_piper()
    assert set(loaded) == {str(env["model"]), str(ar_model)}
    assert loaded[str(env["model"])].calls == [("Ready.", pytest.approx(1.06))]
    (ar_text, ar_scale), = loaded[str(ar_model)].calls
    assert ar_text and ar_scale == pytest.approx(1.06)


def test_length_scale_is_read_from_config(piper_env):
    env, mp = piper_env, piper_env["mp"]
    tts = env["tts"]
    voice = FakeVoice()
    mp.setattr(voice_service, "get_config_value", _cfg(dict(env["base"], PIPER_LENGTH_SCALE="1.2")))
    mp.setattr(tts, "_load_piper_voice", lambda p: voice)
    rendered = tts._piper_render("Hi.")
    assert rendered is not None
    assert voice.calls == [("Hi.", pytest.approx(1.2))]


def test_piper_13_api_receives_synthesis_config_length_scale(tts):
    pytest.importorskip("piper")
    seen = {}

    class Voice13:
        def synthesize(self, text, syn_config=None, include_alignments=False):
            seen["length_scale"] = syn_config.length_scale

            class Chunk:
                audio_int16_bytes = b"\x00\x00" * 10
                sample_rate = 22050

            yield Chunk()

    pcm, rate = tts._piper_synthesize_lib(Voice13(), "Hi.", 1.06)
    assert seen["length_scale"] == pytest.approx(1.06)
    assert rate == 22050 and len(pcm) == 20
