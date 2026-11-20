"""ReminderService (scheduling, firing, persistence, listing, cancelling), the reminder
tools, and recording of approved-tool outcomes in conversation history."""
import json
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import main as main_mod
from src.agent import executor as executor_mod
from src.agent.approvals import CHANNEL_TELEGRAM, ApprovalManager
from src.agent.executor import AgentExecutor
from src.agent.memory import ConversationMemory
from src.agent.permissions import PermissionGate
from src.agent.tools import Tool, ToolCategory, ToolResult, create_default_registry
from src.services import reminder_service as rs
from src.services.reminder_service import (
    Reminder, ReminderError, ReminderService, format_reminder_speech, parse_when,
)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


# a fixed local "now": 2030-06-15 21:00:00
NOW = datetime(2030, 6, 15, 21, 0, 0).timestamp()


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def svc(tmp_path, clock):
    fired = []
    s = ReminderService(tmp_path / "reminders.json", on_fire=fired.append, clock=clock)
    s.fired = fired
    return s


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path))
    rs.reset_reminder_service()
    yield tmp_path
    rs.reset_reminder_service()


# --- parse_when ----------------------------------------------------------------------

def at(h, m, day=15):
    return datetime(2030, 6, day, h, m).timestamp()


@pytest.mark.parametrize("text,expected", [
    ("22:40", at(22, 40)),             # later today
    ("at 22:40", at(22, 40)),
    ("07:30", at(7, 30, day=16)),      # already passed today -> tomorrow
    ("21:00", at(21, 0, day=16)),      # exactly now counts as passed
    ("7:30 pm", at(19, 30, day=16)),
    ("11pm", at(23, 0)),
    ("12 am", at(0, 0, day=16)),
    ("in 10 minutes", NOW + 600),
    ("10 min", NOW + 600),
    ("in 2 hours 30 minutes", NOW + 9000),
    ("1h30m", NOW + 5400),
    ("in 45 seconds", NOW + 45),
])
def test_parse_when_accepts_clock_times_and_delays(text, expected):
    assert parse_when(text, now=NOW) == pytest.approx(expected)


def test_parse_when_delay_seconds_and_iso():
    assert parse_when(None, delay_seconds=90, now=NOW) == NOW + 90
    assert parse_when("2030-06-20T08:15:00", now=NOW) == datetime(2030, 6, 20, 8, 15).timestamp()


@pytest.mark.parametrize("text,delay", [
    ("", None), (None, None), ("banana", None), ("25:00", None), ("13 pm", None),
    ("2030-06-01T08:00:00", None),       # explicit past date is not silently moved
    ("in 0 minutes", None), (None, -5), (None, "soon"), ("in 9999 days", None),
])
def test_parse_when_rejects_bad_input(text, delay):
    with pytest.raises(ReminderError):
        parse_when(text, delay, now=NOW)


# --- service: scheduling / firing -----------------------------------------------------

def test_reminder_fires_only_when_due_and_only_once(svc, clock):
    rem = svc.add("take the pizza out", NOW + 60)
    assert svc.fire_due() == [] and svc.fired == []
    clock.t = NOW + 60
    assert [r.id for r in svc.fire_due()] == [rem.id]
    assert [r.message for r in svc.fired] == ["take the pizza out"]
    assert svc.fire_due() == [] and len(svc.fired) == 1
    assert svc.list() == []


def test_due_reminders_fire_oldest_first(svc, clock):
    svc.add("second", NOW + 20)
    svc.add("first", NOW + 10)
    clock.t = NOW + 100
    svc.fire_due()
    assert [r.message for r in svc.fired] == ["first", "second"]


def test_callback_error_does_not_break_service_or_refire(tmp_path, clock):
    calls = []

    def boom(rem):
        calls.append(rem.id)
        raise RuntimeError("tts exploded")

    s = ReminderService(tmp_path / "r.json", on_fire=boom, clock=clock)
    s.add("a", NOW + 1)
    s.add("b", NOW + 1)
    clock.t = NOW + 5
    assert len(s.fire_due()) == 2 and len(calls) == 2   # b still fires after a's callback failed
    assert s.fire_due() == []


def test_message_is_validated_trimmed_and_redacted(svc):
    with pytest.raises(ReminderError):
        svc.add("   ", NOW + 5)
    rem = svc.add("x" * 2000, NOW + 5)
    assert len(rem.message) == rs.MAX_MESSAGE_CHARS
    leaked = svc.add("use key sk-ant-api03-" + "A" * 40, NOW + 5)
    assert "A" * 10 not in leaked.message


def test_pending_reminders_are_capped(svc, monkeypatch):
    monkeypatch.setattr(rs, "MAX_PENDING", 2)
    svc.add("1", NOW + 5)
    svc.add("2", NOW + 5)
    with pytest.raises(ReminderError):
        svc.add("3", NOW + 5)


def test_background_thread_fires_a_reminder(tmp_path):
    done = threading.Event()
    s = ReminderService(tmp_path / "r.json", on_fire=lambda r: done.set())
    s.start()
    try:
        s.add("soon", time.time() + 0.2)   # added while the thread waits: it must wake up
        assert done.wait(3.0)
    finally:
        s.stop()
    assert s.list() == []


# --- service: listing / cancelling / persistence --------------------------------------

def test_list_is_sorted_and_cancel_removes(svc):
    late = svc.add("late", NOW + 300)
    early = svc.add("early", NOW + 30)
    assert [r.message for r in svc.list()] == ["early", "late"]
    assert svc.cancel(early.id) is True
    assert svc.cancel(early.id) is False and svc.cancel("nope") is False
    assert [r.id for r in svc.list()] == [late.id]


def test_reminders_survive_restart_and_cancel_persists(tmp_path, clock):
    path = tmp_path / "reminders.json"
    first = ReminderService(path, clock=clock)
    keep = first.add("keep me", NOW + 600)
    drop = first.add("drop me", NOW + 700)
    first.cancel(drop.id)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [d["message"] for d in data] == ["keep me"]

    second = ReminderService(path, clock=clock)
    assert [(r.id, r.message, r.due) for r in second.list()] == [(keep.id, "keep me", keep.due)]


def test_fired_reminder_is_not_restored_after_restart(tmp_path, clock):
    path = tmp_path / "reminders.json"
    s = ReminderService(path, clock=clock)
    s.add("once", NOW + 1)
    clock.t = NOW + 2
    s.fire_due()
    assert ReminderService(path, clock=clock).list() == []


def test_overdue_reminder_fires_at_startup_marked_late(tmp_path, clock):
    path = tmp_path / "reminders.json"
    ReminderService(path, clock=clock).add("call mum", at(22, 40))
    clock.t = at(22, 40) + 3600   # Altron was off for an hour
    fired = []
    s = ReminderService(path, on_fire=fired.append, clock=clock)
    s.fire_due()
    assert len(fired) == 1
    assert format_reminder_speech(fired[0], now=clock.t) == "Reminder (missed at 22:40): call mum"
    assert format_reminder_speech(fired[0], now=fired[0].due + 5) == "Reminder: call mum"


def test_corrupt_or_malformed_file_is_ignored(tmp_path, clock):
    path = tmp_path / "reminders.json"
    path.write_text("{not json", encoding="utf-8")
    assert ReminderService(path, clock=clock).list() == []
    path.write_text(json.dumps([{"id": "a"}, {"id": "b", "message": "ok", "due": 5, "created": 1}]), encoding="utf-8")
    assert [r.id for r in ReminderService(path, clock=clock).list()] == ["b"]


# --- agent tools -----------------------------------------------------------------------

def test_reminder_tools_are_registered_and_need_no_approval():
    reg = create_default_registry()
    gate = PermissionGate()
    for name in ("set_reminder", "list_reminders", "cancel_reminder"):
        tool = reg.get(name)
        assert tool is not None and tool.requires_permission is False
        assert gate.is_safe(name) and not gate.requires_permission(name)


def test_set_list_cancel_tools_round_trip(data_dir):
    reg = create_default_registry()
    res = reg.get("set_reminder").run({"time_or_delay": "in 10 minutes", "message": "stretch"})
    assert res.success and res.output["id"]
    rid = res.output["id"]

    listed = reg.get("list_reminders").run({})
    assert listed.success and [r["id"] for r in listed.output["reminders"]] == [rid]
    assert listed.output["reminders"][0]["message"] == "stretch"

    assert (data_dir / "reminders.json").exists()
    assert reg.get("cancel_reminder").run({"reminder_id": rid}).success
    assert reg.get("list_reminders").run({}).output["reminders"] == []
    assert not reg.get("cancel_reminder").run({"reminder_id": rid}).success


def test_set_reminder_tool_supports_delay_seconds_and_reports_errors(data_dir):
    tool = create_default_registry().get("set_reminder")
    assert tool.run({"delay_seconds": 120, "message": "tea"}).success
    bad = tool.run({"time_or_delay": "whenever", "message": "x"})
    assert not bad.success and "understand" in bad.error
    assert not tool.run({"time_or_delay": "in 5 minutes", "message": ""}).success


# --- firing handler wired in main.py ---------------------------------------------------

def test_fire_handler_speaks_logs_to_hud_and_sends_telegram(monkeypatch):
    hud = []
    monkeypatch.setattr(main_mod, "update_hud_status", lambda **k: hud.append(k))
    tts, tg, chime, toast = MagicMock(is_enabled=True, is_muted=False), MagicMock(), MagicMock(), MagicMock()
    main_mod.make_reminder_handler(tts, notify_telegram=tg, chime=chime, notify_desktop=toast,
                                   run_async=False)(Reminder("a1", "drink water", time.time(), time.time()))
    chime.assert_called_once_with()
    tts.speak.assert_called_once_with("Reminder: drink water")
    toast.assert_called_once_with("Altron reminder", "drink water")
    tg.assert_called_once()
    assert "drink water" in tg.call_args.args[0]
    assert hud == [{"log_entry": "Altron: Reminder: drink water"}]


def test_fire_handler_stays_quiet_when_muted_but_still_logs(monkeypatch):
    hud = []
    monkeypatch.setattr(main_mod, "update_hud_status", lambda **k: hud.append(k))
    tts, chime, toast = MagicMock(is_enabled=True, is_muted=True), MagicMock(), MagicMock()
    main_mod.make_reminder_handler(tts, notify_telegram=MagicMock(), chime=chime, notify_desktop=toast,
                                   run_async=False)(Reminder("a1", "x", time.time(), time.time()))
    tts.speak.assert_not_called()
    chime.assert_not_called()
    toast.assert_called_once()  # visual notification still shows
    assert hud and "muted" in hud[0]["log_entry"]


# --- post-approval conversation history ------------------------------------------------

def make_agent(tmp_path, monkeypatch, tool_result):
    monkeypatch.setattr(executor_mod, "update_hud_status", lambda *a, **k: None)
    ex = AgentExecutor(
        ai_client=MagicMock(), database=None, approvals=ApprovalManager(dispatch=lambda fn: fn()),
        memory=ConversationMemory(memory_file=tmp_path / "memory.json"),
    )
    calls = []

    def handler(params, approved=False):
        calls.append((dict(params), approved))
        if isinstance(tool_result, Exception):
            raise tool_result
        return tool_result

    ex.registry.register(Tool(
        name="terminal_execute", description="t", category=ToolCategory.TERMINAL,
        parameters=[], handler=handler, requires_permission=True, is_dangerous=True,
    ))
    ex.calls = calls
    return ex


def approve_and_run(ex, args):
    """Create a Telegram-origin request and approve it the way the inline keyboard does."""
    approvals = ex.approvals
    rid = approvals.create_request("terminal_execute", args, channel=CHANNEL_TELEGRAM, summary="run it")
    req = approvals.get(rid)
    return approvals.resolve(rid, True, CHANNEL_TELEGRAM, req.args_hash)


def test_approved_success_is_recorded_in_memory_and_history(tmp_path, monkeypatch):
    ex = make_agent(tmp_path, monkeypatch, ToolResult(success=True, output={"message": "Created reminder.txt"}))
    ex._remember("user", "create the file")
    ex._remember("assistant", "🔐 Waiting for your approval on screen to run terminal_execute: x.")

    approve_and_run(ex, {"command": "echo hi"})

    assert ex.calls == [({"command": "echo hi"}, True)]
    last = ex.memory.get_recent_messages()[-1]
    assert last["role"] == "assistant"
    assert "terminal_execute completed successfully" in last["content"]
    assert "Created reminder.txt" in last["content"]
    assert ex._history.turns()[-1]["content"] == last["content"]


def test_approved_failure_is_recorded_as_failed(tmp_path, monkeypatch):
    ex = make_agent(tmp_path, monkeypatch, ToolResult(success=False, output=None, error="Access is denied"))
    approve_and_run(ex, {"command": "del x"})
    last = ex.memory.get_recent_messages()[-1]["content"]
    assert "terminal_execute FAILED" in last and "Access is denied" in last


def test_approved_tool_exception_is_recorded_not_lost(tmp_path, monkeypatch):
    ex = make_agent(tmp_path, monkeypatch, RuntimeError("boom"))
    approve_and_run(ex, {"command": "x"})
    last = ex.memory.get_recent_messages()[-1]["content"]
    assert "FAILED" in last and "boom" in last


def test_outcome_note_is_short_redacted_and_next_turn_sees_it(tmp_path, monkeypatch):
    secret = "sk-ant-api03-" + "A" * 40
    ex = make_agent(tmp_path, monkeypatch, ToolResult(success=True, output={"message": f"{secret} " + "y" * 2000}))
    approve_and_run(ex, {"command": "x"})
    stored = ex._history.turns()[-1]["content"]
    assert secret not in stored
    assert len(ex.memory.get_recent_messages()[-1]["content"]) < 400
    # process() rebuilds the live messages from memory, so the next turn sees the outcome
    recent = ex.memory.get_recent_messages(limit=10)
    assert any("[Approved action finished]" in m["content"] for m in recent)
