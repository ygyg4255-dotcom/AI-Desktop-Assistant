"""Phase 2: cache-safe active context, PIN session trust, daily usage tracker."""
import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from src.agent import executor as executor_mod
from src.agent import tools as tools_mod
from src.agent.approvals import TRUSTED_TOOLS, ApprovalManager
from src.agent.control_intents import ControlHandler
from src.agent.executor import AgentExecutor
from src.agent.memory import ConversationMemory
from src.agent.tools import ToolResult
from src.security.pin import hash_pin
from src.services import usage_tracker

PIN = "7391"
HASH = hash_pin(PIN, iterations=1000)


# --- 1. cache prefix --------------------------------------------------------------

def _agent(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_mod, "update_hud_status", lambda *a, **k: None)
    ai = MagicMock()
    ai.make_tool_request.return_value = {"content": [{"type": "text", "text": "ok"}]}
    ex = AgentExecutor(ai_client=ai, database=None, approvals=ApprovalManager(dispatch=lambda fn: fn()),
                       memory=ConversationMemory(memory_file=tmp_path / "memory.json"))
    return ex, ai


def test_system_prompt_is_byte_identical_when_state_changes(tmp_path, monkeypatch):
    ex, _ = _agent(tmp_path, monkeypatch)
    before = ex._get_system_prompt()
    ex._working_state.update("app_open", {"app_name": "calc"}, {})
    ex._working_state.update("window_control", {"action": "focus", "title": "Notepad"}, {"hwnd": 5})
    assert ex._get_system_prompt().encode() == before.encode()


def test_active_context_goes_on_user_turn_only_and_is_not_stored(tmp_path, monkeypatch):
    ex, ai = _agent(tmp_path, monkeypatch)
    ex._working_state.update("app_open", {"app_name": "calc"}, {})
    ex._process_sync("close it")
    payload = ai.make_tool_request.call_args[0][0]
    assert "[Active Context]" in payload["messages"][0]["content"]
    assert "last opened app: calc" in payload["messages"][0]["content"]
    assert "[Active Context]" not in payload["system"]
    assert ex._messages[0].content == "close it"  # stored history stays clean


# --- 2. session trust -------------------------------------------------------------

class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def mgr(monkeypatch):
    monkeypatch.setenv("ALTRON_SESSION_TRUST_SECONDS", "180")
    clock = Clock()
    m = ApprovalManager(runner=lambda t, a: ToolResult(success=True, output={}), clock=clock,
                        dispatch=lambda fn: fn(), pin_hash=lambda: HASH)
    m.clock = clock
    return m


def _approve(m, tool="gui_hotkey"):
    rid = m.create_request(tool, {"keys": ["ctrl", "c"]}, channel="hud")
    return rid, m.verify_pin_and_approve(rid, PIN, "hud")


def test_pin_approval_grants_trust_for_medium_risk_only(mgr):
    assert not mgr.trust_active("gui_type")
    _approve(mgr)
    for tool in TRUSTED_TOOLS:
        assert mgr.trust_active(tool)
    for tool in ("file_delete", "file_move", "file_write", "code_edit", "terminal_execute", "gmail_send_email",
                 "power_control", "app_close", "window_close", "browser_close_tab"):
        assert not mgr.trust_active(tool)


def test_trust_expires(mgr):
    _approve(mgr)
    mgr.clock.t += 181
    assert not mgr.trust_active("gui_hotkey")


def test_trust_disabled_with_zero(mgr, monkeypatch):
    monkeypatch.setenv("ALTRON_SESSION_TRUST_SECONDS", "0")
    _approve(mgr)
    assert not mgr.trust_active("gui_hotkey")


def test_destructive_approval_does_not_grant_trust(mgr):
    _approve(mgr, "file_delete")
    assert not mgr.trust_active("gui_hotkey")


def test_trust_revoked_on_cancel_wrong_pin_and_control_command(mgr):
    _approve(mgr)
    rid = mgr.create_request("file_delete", {"path": "x"}, channel="hud")
    mgr.cancel(rid, "hud")
    assert not mgr.trust_active("gui_hotkey")

    _approve(mgr)
    rid = mgr.create_request("gui_type", {"text": "a"}, channel="hud")
    assert mgr.verify_pin_and_approve(rid, "0000", "hud").outcome == "wrong_pin"
    assert not mgr.trust_active("gui_hotkey")

    _approve(mgr)
    assert mgr.trust_active("gui_hotkey")
    h = ControlHandler(lambda: None, lambda: None, lambda: None, lambda: None, revoke_trust=mgr.revoke_trust)
    assert "PIN" in h.handle("lock")
    assert not mgr.trust_active("gui_hotkey")


def test_no_trust_without_a_pin(mgr):
    _approve(mgr)
    mgr._pin_hash = lambda: ""
    assert not mgr.trust_active("gui_hotkey")


def test_executor_skips_card_inside_window_but_not_for_destructive(tmp_path, monkeypatch, mgr):
    ex, _ = _agent(tmp_path, monkeypatch)
    ex._approvals = mgr
    ran = []

    def handler(params, approved=False):
        if not approved:
            return ToolResult(success=False, output=None, requires_permission=True, is_dangerous=True, permission_message="x")
        ran.append(approved)
        return ToolResult(success=True, output={"message": "done"})

    for name in ("gui_hotkey", "file_delete"):
        ex._registry.get(name).handler = handler
    _approve(mgr)
    assert ex._execute_tool_with_permission("gui_hotkey", {"keys": "ctrl+c"}).success
    assert ran == [True]
    assert ex._execute_tool_with_permission("file_delete", {"path": "x"}).requires_permission


# --- 3. usage tracker -------------------------------------------------------------

USAGE = {"input_tokens": 1000, "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 7000, "output_tokens": 500}


def test_record_and_cost(tmp_path):
    usage_tracker.record("claude-sonnet-5-5", USAGE)
    usage_tracker.record("claude-sonnet-5-5", USAGE)
    data = json.loads((tmp_path / "altron-data" / "usage_stats.json").read_text())
    row = data[date.today().isoformat()]["claude-sonnet-5-5"]
    assert row["calls"] == 2 and row["cache_read"] == 14000 and row["output"] == 1000
    one = (1000 * 3 + 2000 * 3.75 + 7000 * 0.3 + 500 * 15) / 1e6
    assert row["cost"] == pytest.approx(2 * one)


def test_model_price_tiers():
    assert usage_tracker.estimate_cost("claude-haiku-5-5", 1_000_000, 0, 0, 0) == pytest.approx(1.0)
    assert usage_tracker.estimate_cost("claude-opus-5-5", 0, 0, 0, 1_000_000) == pytest.approx(75.0)


def test_usage_summary_tool(monkeypatch):
    usage_tracker.record("claude-sonnet-5-5", USAGE)
    r = tools_mod.handle_usage_summary({"days": 7})
    t = r.output["models"]["claude-sonnet-5-5"]
    assert r.success and t["calls"] == 1 and t["cache_hit_rate"] == "70%" and t["cost_usd"] > 0
    assert r.output["total_cost_usd"] == t["cost_usd"] and "notice" not in r.output


def test_budget_notice_and_warning(monkeypatch, caplog):
    monkeypatch.setenv("ALTRON_DAILY_BUDGET_USD", "0.001")
    monkeypatch.setattr(usage_tracker, "_warned_day", "")
    with caplog.at_level("WARNING"):
        usage_tracker.record("claude-sonnet-5-5", USAGE)
    assert "exceeds the daily budget" in caplog.text
    assert "notice" in usage_tracker.summarize(1)


def test_usage_summary_is_safe_tier_and_registered():
    from src.agent.permissions import PermissionGate
    assert PermissionGate().is_safe("usage_summary")
    from src.agent.tools import ToolRegistry
    assert tools_mod.create_default_registry().get("usage_summary") is not None


def test_log_usage_records(tmp_path):
    from src.services.ai_client import log_usage
    log_usage(USAGE, "claude-haiku-5-5")
    assert "claude-haiku-5-5" in json.loads((tmp_path / "altron-data" / "usage_stats.json").read_text())[date.today().isoformat()]
