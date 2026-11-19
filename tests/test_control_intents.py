"""Shut down / mute / forget commands: strict matching and the actions they trigger."""
import pytest

from src.agent.control_intents import (
    CLEAR_MEMORY, MUTE, SHUTDOWN, UNMUTE, ControlHandler, match_control_intent,
)


@pytest.mark.parametrize("text", [
    "close yourself", "Close yourself.", "shut yourself down", "Shut down Altron!",
    "please close yourself", "Altron, close yourself", "hey altron quit altron",
    "Altron, shut down", "اغلق نفسك", "أغلق ألترون",
])
def test_shutdown_phrases(text):
    assert match_control_intent(text) == SHUTDOWN


@pytest.mark.parametrize("text,expected", [
    ("stop listening", MUTE), ("Mute yourself.", MUTE), ("please mute the mic", MUTE),
    ("pause listening", MUTE), ("توقف عن الاستماع", MUTE),
    ("resume listening", UNMUTE), ("unmute the microphone", UNMUTE),
    ("clear memory", CLEAR_MEMORY), ("forget the previous session", CLEAR_MEMORY),
    ("Forget previous session!", CLEAR_MEMORY), ("امسح الذاكرة", CLEAR_MEMORY),
])
def test_other_control_phrases(text, expected):
    assert match_control_intent(text) == expected


@pytest.mark.parametrize("text", [
    "shut down",                       # could mean the PC: needs 'Altron' or 'yourself'
    "mute",                            # volume mute, handled by the local intent router
    "I'm sorry.", "Thank you.", "",    # typical Whisper noise hallucinations
    "stop listening to the radio and play jazz",
    "can you close yourself in the other window",
    "please shut down the computer",
    "forget it", "clear",
])
def test_ordinary_speech_and_noise_never_trigger(text):
    assert match_control_intent(text) is None


class Fakes:
    def __init__(self):
        self.log = []

    def handler(self):
        return ControlHandler(
            clear_memory=lambda: self.log.append("clear"),
            request_shutdown=lambda: self.log.append("shutdown"),
            pause_listening=lambda: self.log.append("pause"),
            resume_listening=lambda: self.log.append("resume"),
        )


def test_shutdown_requests_a_graceful_exit_and_says_goodbye():
    f = Fakes()
    reply = f.handler().handle("close yourself")
    assert f.log == ["shutdown"] and "Goodbye" in reply


def test_mute_pauses_the_mic_only_and_tracks_state():
    f = Fakes()
    h = f.handler()
    reply = h.handle("stop listening")
    assert f.log == ["pause"] and h.muted is True
    assert "HUD" in reply and "Telegram" in reply         # other channels stay active
    h.handle("resume listening")
    assert f.log == ["pause", "resume"] and h.muted is False


def test_forget_clears_saved_memory():
    f = Fakes()
    reply = f.handler().handle("forget the previous session")
    assert f.log == ["clear"] and "cleared" in reply


def test_non_control_text_is_passed_through_untouched():
    f = Fakes()
    assert f.handler().handle("open notepad") is None
    assert f.log == []


def test_arabic_commands_get_arabic_replies():
    f = Fakes()
    reply = f.handler().handle("اغلق نفسك")
    assert f.log == ["shutdown"] and any("؀" <= c <= "ۿ" for c in reply)


def test_clear_history_wires_to_executor_reset(tmp_path, monkeypatch):
    """The 'clear memory' command ends up wiping both working memory and saved history."""
    from unittest.mock import MagicMock
    from src.agent import executor as executor_mod
    from src.agent.executor import AgentExecutor
    from src.agent.history import PersistentHistory
    from src.agent.memory import ConversationMemory

    monkeypatch.setattr(executor_mod, "update_hud_status", lambda *a, **k: None)
    hist = PersistentHistory(tmp_path / "conversation_history.json")
    hist.append("user", "old preference")
    ex = AgentExecutor(ai_client=MagicMock(), database=None,
                       memory=ConversationMemory(memory_file=tmp_path / "memory.json"), history=hist)
    handler = ControlHandler(ex.clear_history, lambda: None, lambda: None, lambda: None)
    handler.handle("forget previous session")
    assert hist.turns() == []
