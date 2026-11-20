"""Prompt and loop-budget guards for autonomous multi-step execution."""
from pathlib import Path

PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "system_prompt.txt"


def _text() -> str:
    return PROMPT.read_text(encoding="utf-8")


def test_autonomous_rule_present():
    text = _text()
    assert "AUTONOMOUS MULTI-STEP EXECUTION" in text
    assert "ALL the required safe steps" in text
    assert "genuinely ambiguous" in text


def test_steam_cart_uses_single_atomic_tool():
    text = _text()
    assert "call `steam_add_to_cart`" in text
    assert "NEVER click Checkout" in text
    assert "if you'd like" in text  # only inside the "never say this" instruction


def test_executor_iteration_budget_fits_steam_flow():
    src = (Path(__file__).resolve().parents[1] / "src" / "agent" / "executor.py").read_text(encoding="utf-8")
    import re
    m = re.search(r"self\._max_iterations = (\d+)", src)
    assert m and int(m.group(1)) >= 10
