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


def test_steam_loop_is_explicit():
    text = _text()
    for needle in ("steam_search_store", "steam_open_store_page(appid)",
                   "capture_and_inspect_screen", "gui_scroll", "up to 4 times",
                   "gui_click", "Never click Checkout"):
        assert needle in text


def test_executor_iteration_budget_fits_steam_flow():
    src = (Path(__file__).resolve().parents[1] / "src" / "agent" / "executor.py").read_text(encoding="utf-8")
    import re
    m = re.search(r"self\._max_iterations = (\d+)", src)
    assert m and int(m.group(1)) >= 10
