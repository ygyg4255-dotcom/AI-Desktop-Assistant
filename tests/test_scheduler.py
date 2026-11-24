"""Silent scheduler: relative/absolute scheduling, silent firing (no TTS), persistence across
restart, cancellation, permission enforcement for deferred actions, context block, HUD API."""
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import ui_server
from src.agent.permissions import PermissionGate
from src.agent.tools import (
    Tool, ToolCategory, ToolRegistry, ToolResult, create_default_registry, run_scheduled_action,
)
from src.services import hud_status, scheduler as sch
from src.services.scheduler import SchedulerService, format_block, get_scheduler, reset_scheduler


@pytest.fixture
def svc():
    reset_scheduler()
    yield get_scheduler()
    reset_scheduler()


@pytest.fixture
def tools():
    return create_default_registry()


def _run(tools, name, approved=False, **params):
    return tools.get(name).run(params, approved=approved)


def _iso_epoch(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _skew(svc, seconds):
    svc._clock = lambda: time.time() + seconds  # pretend time has passed


# --- scheduling -------------------------------------------------------------------

@pytest.mark.parametrize("when,secs", [("1h", 3600), ("30m", 1800), ("in 10 minutes", 600)])
def test_schedule_relative(tools, svc, when, secs):
    res = _run(tools, "schedule_task", title="stretch", run_at_or_delay=when)
    assert res.success, res.error
    task = svc.list_pending()[0]
    assert abs(_iso_epoch(task["run_at"]) - (time.time() + secs)) < 5
    assert task["kind"] == "reminder"


def test_schedule_absolute_and_bad_input(tools, svc):
    local = datetime.fromtimestamp(time.time() + 30 * 86400).replace(hour=10, minute=0, second=0, microsecond=0)
    assert _run(tools, "schedule_task", title="a", run_at_or_delay=local.strftime("%Y-%m-%d %H:%M")).success
    assert _iso_epoch(svc.list_pending()[0]["run_at"]) == local.timestamp()
    assert _run(tools, "schedule_task", title="b", run_at_or_delay="10:00").success  # clock time, rolls to tomorrow if past
    assert not _run(tools, "schedule_task", title="c", run_at_or_delay="whenever").success
    assert not _run(tools, "schedule_task", title="", run_at_or_delay="1h").success
    assert len(svc.list_pending()) == 2


# --- firing is silent --------------------------------------------------------------

def test_reminder_fires_silently_without_tts(tools, svc, monkeypatch):
    from src.services import voice_service
    speak = MagicMock()
    monkeypatch.setattr(voice_service.TTSManager, "speak", speak)
    telegram = MagicMock()
    svc.set_callbacks(on_notify=lambda title, text: (hud_status.push_hud_toast(text), telegram(text)))
    _run(tools, "schedule_task", title="drink water", run_at_or_delay="1m")
    assert svc.fire_due() == 0  # not due yet
    _skew(svc, 120)
    assert svc.fire_due() == 1
    assert svc.fire_due() == 0  # fires once
    speak.assert_not_called()
    telegram.assert_called_once()
    assert "drink water" in hud_status.get_hud_status()["toast"]["text"]
    assert svc.list_pending() == []


# --- persistence across restart ----------------------------------------------------

def test_persists_across_restart_and_marks_missed(tmp_path):
    path = tmp_path / "s.db"
    now = [1_000_000.0]
    a = SchedulerService(path, clock=lambda: now[0])
    a.add("reminder", "future", now[0] + 3600)
    a.add("reminder", "late reminder", now[0] + 60)
    a.add("action", "late action", now[0] + 60, {"action_name": "power_control", "action_args": {"action": "lock"}})
    del a
    notes, runner = [], MagicMock()
    now[0] += 7200  # Altron was off for two hours
    b = SchedulerService(path, on_notify=lambda t, x: notes.append(x), run_action=runner, clock=lambda: now[0])
    assert len(b.list_pending()) == 3  # reloaded from disk
    b.fire_due()
    assert any("missed" in n and "late reminder" in n for n in notes)  # reminder still delivered, flagged
    assert any("Skipped" in n and "late action" in n for n in notes)
    runner.assert_not_called()  # a stale action is never run
    assert b.list_pending() == []  # all three were due after the 2h gap, none left pending


def test_future_task_survives_restart(tmp_path):
    path = tmp_path / "s.db"
    SchedulerService(path).add("reminder", "later", time.time() + 3600)
    assert [t["title"] for t in SchedulerService(path).list_pending()] == ["later"]


# --- cancellation ------------------------------------------------------------------

def test_cancel_by_id_and_keyword(tools, svc):
    _run(tools, "schedule_task", title="Shutdown PC", run_at_or_delay="1h", kind="action",
         action_name="power_control", action_args={"action": "shutdown"}, approved=True)
    _run(tools, "schedule_task", title="call mom", run_at_or_delay="2h")
    _run(tools, "schedule_task", title="call dad", run_at_or_delay="3h")
    listed = _run(tools, "list_scheduled_tasks").output["tasks"]
    assert [t["title"] for t in listed] == ["Shutdown PC", "call mom", "call dad"]
    assert "Shutdown PC" in _run(tools, "cancel_scheduled_task", task_id_or_query="shutdown timer").output["message"]
    ambiguous = _run(tools, "cancel_scheduled_task", task_id_or_query="call")
    assert not ambiguous.success and "Several" in ambiguous.error
    assert _run(tools, "cancel_scheduled_task", task_id_or_query=f"#{listed[1]['id']}").success
    assert [t["title"] for t in svc.list_pending()] == ["call dad"]
    assert not _run(tools, "cancel_scheduled_task", task_id_or_query="nothing here").success


# --- permission enforcement --------------------------------------------------------

def test_shutdown_needs_approval_at_scheduling_time(tools, svc):
    kw = dict(title="Shutdown", run_at_or_delay="1h", kind="action", action_name="power_control",
              action_args={"action": "shutdown"})
    res = _run(tools, "schedule_task", **kw)
    assert res.requires_permission and res.is_dangerous and svc.list_pending() == []
    assert _run(tools, "schedule_task", approved=True, **kw).success
    assert len(svc.list_pending()) == 1
    assert PermissionGate().refuses_blanket_allow("schedule_task")  # no "allow always"


def test_other_actions_follow_their_tier(tools, svc):
    ask = dict(title="x", run_at_or_delay="1h", kind="action")
    assert _run(tools, "schedule_task", action_name="terminal_execute", action_args={"command": "dir"}, **ask).requires_permission
    assert _run(tools, "schedule_task", action_name="power_control", action_args={"action": "lock"}, **ask).success  # no approval needed
    for bad in ("file_read", "nonexistent_tool", "schedule_task", ""):
        res = _run(tools, "schedule_task", action_name=bad, action_args={}, **ask)
        assert not res.success and not res.requires_permission, bad


def test_fire_time_never_grants_missing_approval(svc):
    spy = MagicMock(return_value=ToolResult(success=True, output={"message": "ok"}))

    def handler(params, approved=False):
        return spy(params, approved)

    reg = ToolRegistry()
    reg.register(Tool("power_control", "p", ToolCategory.SYSTEM, [], handler, requires_permission=True))
    notes = []
    svc.set_callbacks(on_notify=lambda t, x: notes.append(x),
                      run_action=lambda n, a, ap: run_scheduled_action(reg, n, a, ap))
    shutdown = {"action_name": "power_control", "action_args": {"action": "shutdown"}}
    svc.add("action", "unapproved shutdown", time.time() + 1, {**shutdown, "approved": False})  # e.g. tampered DB row
    svc.add("action", "approved shutdown", time.time() + 1, {**shutdown, "approved": True})
    _skew(svc, 10)
    svc.fire_due()
    assert spy.call_count == 1 and spy.call_args[0][1] is True
    assert any("Failed: unapproved shutdown" in n for n in notes)
    assert any("Done: approved shutdown" in n for n in notes)


# --- context block + HUD API -------------------------------------------------------

def test_context_block_is_compact_and_capped(svc):
    for i in range(7):
        svc.add("reminder", f"task {i}", time.time() + 600 * (i + 1))
    block = format_block(svc.list_pending(limit=5))
    assert block.count("\n- #") == 5 and "task 0" in block and "task 6" not in block
    assert format_block([]) == ""


def test_hud_scheduled_endpoints(svc, monkeypatch):
    monkeypatch.setattr(ui_server, "_HUD_PORT", 5000)
    hud, base, h = ui_server.flask_app.test_client(), "http://127.0.0.1:5000", {"X-HUD-Token": ui_server._HUD_TOKEN}
    task_id = svc.add("reminder", "hud task", time.time() + 600)
    assert hud.get("/scheduled", base_url=base).status_code == 401
    assert [t["title"] for t in hud.get("/scheduled", base_url=base, headers=h).get_json()["tasks"]] == ["hud task"]
    assert hud.delete(f"/scheduled/{task_id}", base_url=base).status_code == 401
    assert hud.delete(f"/scheduled/{task_id}", base_url=base, headers=h).status_code == 200
    assert hud.delete(f"/scheduled/{task_id}", base_url=base, headers=h).status_code == 404
