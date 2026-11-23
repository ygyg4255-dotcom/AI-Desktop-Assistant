"""Compound requests run every step; an approval mid-task resumes the rest."""
from unittest.mock import MagicMock

import pytest

import src.agent.executor as executor_mod
from src.agent.approvals import ApprovalManager
from src.agent.executor import AgentExecutor
from src.agent.memory import ConversationMemory
from src.agent.tools import Tool, ToolCategory, ToolParameter, ToolResult

MSG = "close WhatsApp, hide your window, and send me a screenshot"


def _tu(i, name):
    return {"content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": {}}]}


def _text(t):
    return {"content": [{"type": "text", "text": t}]}


def _tool(name, calls, gated=False):
    def handler(params, approved=False):
        if gated and not approved:
            return ToolResult(success=False, output=None, requires_permission=True, permission_message="needs PIN")
        calls.append(name)
        return ToolResult(success=True, output={"message": f"{name} done"})
    return Tool(name=name, description="t", category=ToolCategory.UTILITY, parameters=[],
                handler=handler, requires_permission=gated)


@pytest.fixture
def make_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(executor_mod, "update_hud_status", lambda *a, **k: None)

    def make(gated_name=None):
        ex = AgentExecutor(ai_client=MagicMock(), database=None,
                           approvals=ApprovalManager(dispatch=lambda fn: fn()),
                           memory=ConversationMemory(memory_file=tmp_path / "memory.json"))
        ex.calls = []
        for n in ("app_close", "window_hide", "capture_and_inspect_screen"):
            ex.registry.register(_tool(n, ex.calls, gated=(n == gated_name)))
        return ex
    return make


def test_three_step_task_runs_all_tools_then_one_summary(make_agent):
    ex = make_agent()
    ex._ai_client.make_tool_request.side_effect = [
        _tu(1, "app_close"), _tu(2, "window_hide"), _tu(3, "capture_and_inspect_screen"),
        _text("Closed WhatsApp, hid the window and sent the screenshot."),
    ]
    resp = ex._process_sync(MSG)
    assert ex.calls == ["app_close", "window_hide", "capture_and_inspect_screen"]
    assert resp.tools_used == ex.calls
    assert "screenshot" in resp.content


def test_approved_step_resumes_remaining_steps(make_agent):
    ex = make_agent(gated_name="window_hide")
    ex._ai_client.make_tool_request.side_effect = [
        _tu(1, "app_close"), _tu(2, "window_hide"),
        # after approval the loop resumes:
        _tu(3, "capture_and_inspect_screen"), _text("All three steps done."),
    ]
    done = []
    ex.on_resume_complete = lambda res, origin: done.append(res.content)

    resp = ex._process_sync(MSG)
    assert resp.error == "awaiting approval" and ex.calls == ["app_close"]

    (req,) = ex.approvals.list_pending()
    ex.approvals.resolve(req["id"], True, "hud", req["args_hash"])  # dispatch is inline

    assert ex.calls == ["app_close", "window_hide", "capture_and_inspect_screen"]
    assert done == ["All three steps done."]
    # the resumed turn was told what already ran, so it does not repeat step 1
    resumed = ex._ai_client.make_tool_request.call_args_list[2].args[0]["messages"][-1]["content"]
    assert "app_close ok" in str(resumed)
