"""The persona/system prompt must demand concise, filler-free answers."""
from pathlib import Path

from src.agent.executor import AgentExecutor

PERSONA = Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.txt"


def test_persona_no_longer_requires_filler_openings():
    text = PERSONA.read_text(encoding="utf-8")
    assert "Frequently open your responses" not in text
    assert "Do NOT open replies with filler" in text
    assert "Answer ONLY what was asked" in text
    assert "Do NOT describe the background" in text


def test_executor_prompt_demands_short_direct_answers():
    prompt = AgentExecutor.SYSTEM_PROMPT
    assert "Always explain what you're doing before using a tool" not in prompt
    assert "1-2 short sentences" in prompt
    assert "answer ONLY the question asked" in prompt
