from pathlib import Path

from src.agent.executor import AgentExecutor
from src.agent.permissions import PermissionGate


def _summary():
    ex = AgentExecutor.__new__(AgentExecutor)
    ex._gate = PermissionGate()
    return ex._approval_summary()


def test_summary_matches_permissions():
    gated, rest = _summary().split("Sometimes gated")[0], _summary()
    for tool in PermissionGate.DANGEROUS_TOOLS:
        assert tool in gated
    assert "window_close" not in gated and "app_close" not in gated  # closing needs no approval
    for safe in ("gmail_read_message", "gmail_list_messages"):
        assert safe not in gated and safe in rest


def test_system_prompt_cap():
    assert len(Path("prompts/system_prompt.txt").read_text(encoding="utf-8")) < 6900


def test_process_list_schema_demands_name_filter():
    from src.agent.tools import create_default_registry
    tool = create_default_registry().get("process_list")
    assert "MUST pass name_filter" in tool.description
    assert "REQUIRED" in tool.parameters[0].description
