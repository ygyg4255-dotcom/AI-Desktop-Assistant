"""Screen vision (compressed multimodal screenshots) and S4-gated GUI automation."""
import base64
import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from src.agent import tools as tools_mod
from src.agent import vision
from src.agent.executor import AgentExecutor
from src.agent.history import PersistentHistory
from src.agent.memory import ConversationMemory
from src.agent.permissions import PermissionGate, PermissionLevel
from src.agent.tools import ToolResult, create_default_registry

GUI_TOOLS = ("gui_click", "gui_type", "gui_hotkey", "gui_scroll")


# ---------------------------------------------------------------- compression

def _decode(block) -> Image.Image:
    assert block["source"]["type"] == "base64"
    return Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))


def test_large_screenshot_is_resized_and_jpeg_encoded():
    big = Image.new("RGBA", (2560, 1440), (200, 30, 30, 255))
    data = vision.compress_image(big)
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    assert img.width == 1280 and img.height == 720  # aspect ratio kept


def test_small_screenshot_is_not_upscaled():
    img = Image.open(io.BytesIO(vision.compress_image(Image.new("RGB", (800, 600)))))
    assert img.size == (800, 600)


def test_image_block_is_anthropic_multimodal_format():
    block = vision.image_block(vision.compress_image(Image.new("RGB", (100, 100))))
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/jpeg"
    assert _decode(block).format == "JPEG"


# ------------------------------------------------------------- capture handler

@pytest.fixture
def fake_screen(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    import PIL.ImageGrab
    monkeypatch.setattr(PIL.ImageGrab, "grab", lambda **kw: Image.new("RGB", (3000, 1600), (10, 120, 200)))


def test_capture_tool_returns_compressed_image_block_outside_output(fake_screen):
    result = tools_mod.handle_capture_and_inspect_screen({"send_to_telegram": False})

    assert result.success and result.image is not None
    assert _decode(result.image).width <= vision.MAX_WIDTH
    assert result.output["image_attached"] is True
    # the base64 payload must not leak into the JSON text handed to the model or logs
    assert result.image["source"]["data"] not in json.dumps(result.output)
    assert "data" not in repr(result).split("image=")[-1][:10]  # image excluded from repr


def test_capture_tool_still_sends_to_telegram(fake_screen, monkeypatch):
    sent = MagicMock()
    monkeypatch.setattr(tools_mod, "send_telegram_photo_async", sent)
    monkeypatch.setattr(tools_mod, "_telegram_credentials", lambda: ("tok", "123"))

    result = tools_mod.handle_capture_and_inspect_screen({"send_to_telegram": True})

    assert result.output["sent_to_telegram"] is True
    sent.assert_called_once()
    assert sent.call_args.kwargs["image_path"].endswith(".png")  # full-res original, unchanged


# ------------------------------------------------- executor + persistent history

def _executor(tmp_path):
    memory = ConversationMemory(tmp_path / "memory.json")
    history = PersistentHistory(tmp_path / "conversation_history.json")
    return AgentExecutor(ai_client=MagicMock(), database=None, memory=memory, history=history), memory, history


def _shot_result() -> ToolResult:
    return ToolResult(
        success=True,
        output={"active_window": "Notepad", "message": "ok"},
        image=vision.image_block(vision.compress_image(Image.new("RGB", (640, 360)))),
    )


def test_feedback_message_carries_image_block_then_text(tmp_path):
    ex, _, _ = _executor(tmp_path)
    ex._append_tool_feedback("capture_and_inspect_screen", "{}", _shot_result())

    content = ex._messages[-1].content
    assert isinstance(content, list)
    assert content[0]["type"] == "image" and content[1]["type"] == "text"


def test_only_latest_screenshot_stays_in_messages(tmp_path):
    ex, _, _ = _executor(tmp_path)
    ex._append_tool_feedback("capture_and_inspect_screen", "{}", _shot_result())
    ex._append_tool_feedback("capture_and_inspect_screen", "{}", _shot_result())

    images = [b for m in ex._messages if isinstance(m.content, list) for b in m.content if b["type"] == "image"]
    assert len(images) == 1
    assert ex._messages[0].content[0] == {"type": "text", "text": vision.SCREENSHOT_PLACEHOLDER}


def test_full_turn_sends_image_to_model_and_never_persists_it(tmp_path, monkeypatch):
    ex, memory, history = _executor(tmp_path)
    shot = _shot_result()
    b64 = shot.image["source"]["data"]
    monkeypatch.setattr(ex, "_execute_tool_with_permission", lambda *a, **k: shot)

    payloads = []
    replies = iter([
        {"content": [{"type": "tool_use", "id": "t1", "name": "capture_and_inspect_screen", "input": {}}]},
        {"content": [{"type": "text", "text": "I can see Notepad with an empty document."}]},
    ])

    def fake_request(payload, **kw):
        payloads.append({"messages": json.loads(json.dumps(payload["messages"]))})  # snapshot: list is mutated later
        return next(replies)

    monkeypatch.setattr(ex, "_make_tool_request", fake_request)

    response = ex._process_sync("what is on my screen?")

    # not short-circuited by the direct-action fast path: the model got a second call with the image
    assert len(payloads) == 2
    last_user = payloads[1]["messages"][-1]
    assert [b["type"] for b in last_user["content"]] == ["image", "text"]
    assert last_user["content"][0]["source"]["data"] == b64
    assert "Notepad" in response.content

    for path in (memory.path, history.path):
        assert b64[:200] not in path.read_text(encoding="utf-8"), f"screenshot data leaked into {path.name}"


def test_persistent_stores_scrub_base64_blobs(tmp_path):
    blob = base64.b64encode(b"\xff\xd8\xff" * 4000).decode()
    history = PersistentHistory(tmp_path / "h.json")
    history.append("assistant", f"saw this: {blob}")
    memory = ConversationMemory(tmp_path / "m.json")
    memory.add_message("assistant", f"saw this: {blob}")

    for text in (history.path.read_text(encoding="utf-8"), memory.path.read_text(encoding="utf-8")):
        assert blob[:100] not in text
        assert vision.SCREENSHOT_PLACEHOLDER in text


def test_strip_image_data_replaces_blocks_with_placeholder():
    blocks = [vision.image_block(b"abc"), {"type": "text", "text": "hi"}]
    assert vision.strip_image_data(blocks) == [
        {"type": "text", "text": vision.SCREENSHOT_PLACEHOLDER}, {"type": "text", "text": "hi"}]
    assert vision.strip_image_data("plain") == "plain"


# ------------------------------------------------------------------ GUI tools

@pytest.fixture
def pg(monkeypatch):
    """A mock pyautogui whose FAILSAFE/PAUSE start in the *unsafe* state."""
    mock = MagicMock()
    mock.FAILSAFE = False
    mock.PAUSE = 0
    mock.size.return_value = (1920, 1080)
    monkeypatch.setitem(__import__("sys").modules, "pyautogui", mock)
    monkeypatch.setattr(tools_mod, "_foreground_window_title", lambda: "Untitled - Notepad")
    return mock


GUI_CALLS = [
    ("gui_click", {"x": 100, "y": 200}),
    ("gui_type", {"text": "hello"}),
    ("gui_hotkey", {"keys": ["ctrl", "c"]}),
    ("gui_scroll", {"amount": -3}),
]


@pytest.mark.parametrize("name, args", GUI_CALLS)
def test_gui_tools_are_dangerous_and_registered_with_permission(name, args):
    gate = PermissionGate()
    tool = create_default_registry().get(name)
    assert tool is not None and tool.requires_permission and tool.is_dangerous
    assert gate.is_dangerous(name) and not gate.is_safe(name) and not gate.is_conditional(name)
    assert gate.get_permission_level(name) == PermissionLevel.ASK


@pytest.mark.parametrize("name, args", GUI_CALLS)
def test_gui_tools_ask_instead_of_acting_without_approval(pg, name, args):
    result = create_default_registry().get(name).run(args)  # approved=False

    assert result.requires_permission and result.is_dangerous and result.permission_message
    assert pg.mock_calls == [] or all(c[0].startswith("size") for c in pg.mock_calls)
    for fn in (pg.click, pg.write, pg.hotkey, pg.scroll, pg.press):
        fn.assert_not_called()


@pytest.mark.parametrize("name, args", GUI_CALLS)
def test_model_cannot_forge_approval_for_gui_tools(pg, name, args):
    forged = {**args, "approved": True, "_approved": True}
    result = create_default_registry().get(name).run(forged)

    assert result.requires_permission
    for fn in (pg.click, pg.write, pg.hotkey, pg.scroll):
        fn.assert_not_called()


@pytest.mark.parametrize("name, args", GUI_CALLS)
def test_blanket_allow_is_refused_for_gui_tools(name, args):
    gate = PermissionGate()
    gate.set_session_permission(name, PermissionLevel.ALLOW)
    assert gate.get_permission_level(name) == PermissionLevel.ASK


def test_executor_gates_gui_tool_through_permission_flow(pg, tmp_path):
    ex, _, _ = _executor(tmp_path)
    callback = MagicMock()
    result = ex._execute_tool_with_permission("gui_click", {"x": 5, "y": 5, "_approved": True}, callback)

    assert result.requires_permission
    callback.assert_called_once()
    pg.click.assert_not_called()


def test_approved_calls_execute_with_failsafe_and_pause(pg):
    reg = create_default_registry()
    assert pg.FAILSAFE is False

    assert reg.get("gui_click").run({"x": 100, "y": 200, "button": "right", "clicks": 2}, approved=True).success
    pg.click.assert_called_once_with(x=100, y=200, button="right", clicks=2)
    assert pg.FAILSAFE is True and pg.PAUSE == 0.1

    assert reg.get("gui_type").run({"text": "hi", "press_enter": True}, approved=True).success
    pg.write.assert_called_once_with("hi", interval=0.02)
    pg.press.assert_called_once_with("enter")

    assert reg.get("gui_hotkey").run({"keys": ["Ctrl", "C"]}, approved=True).success
    pg.hotkey.assert_called_once_with("ctrl", "c")

    assert reg.get("gui_scroll").run({"amount": -5}, approved=True).success
    pg.scroll.assert_called_once_with(-5)


def test_failsafe_exception_is_reported_not_raised(pg):
    class FailSafeException(Exception):
        pass
    pg.click.side_effect = FailSafeException("corner")

    result = create_default_registry().get("gui_click").run({"x": 1, "y": 1}, approved=True)
    assert not result.success and "fail-safe" in result.error


@pytest.mark.parametrize("name, args", [
    ("gui_click", {"x": "a", "y": 1}),
    ("gui_click", {"x": 1, "y": 1, "button": "laser"}),
    ("gui_click", {"x": 1, "y": 1, "clicks": 9}),
    ("gui_type", {"text": ""}),
    ("gui_type", {"text": "x" * 501}),
    ("gui_type", {"text": "مرحبا"}),
    ("gui_hotkey", {"keys": []}),
    ("gui_hotkey", {"keys": ["ctrl", "alt", "shift", "win", "x"]}),
    ("gui_hotkey", {"keys": ["ctrl", "launchmissiles"]}),
    ("gui_scroll", {"amount": 0}),
    ("gui_scroll", {"amount": 99999}),
])
def test_invalid_gui_input_fails_without_prompting(pg, name, args):
    result = create_default_registry().get(name).run(args, approved=True)
    assert not result.success and not result.requires_permission and result.error


def test_click_outside_screen_is_rejected(pg):
    result = create_default_registry().get("gui_click").run({"x": 5000, "y": 10}, approved=True)
    assert not result.success and "outside" in result.error
    pg.click.assert_not_called()


def test_gui_type_refuses_credential_windows_and_secrets(pg, monkeypatch):
    reg = create_default_registry()
    monkeypatch.setattr(tools_mod, "_foreground_window_title", lambda: "Sign in to your account - Edge")
    for approved in (False, True):  # refused before it even asks
        result = reg.get("gui_type").run({"text": "hunter2"}, approved=approved)
        assert not result.success and not result.requires_permission and "sign-in" in result.error
    pg.write.assert_not_called()

    monkeypatch.setattr(tools_mod, "_foreground_window_title", lambda: "Notepad")
    result = reg.get("gui_type").run({"text": "key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}, approved=True)
    assert not result.success and "secret" in result.error
    pg.write.assert_not_called()


def test_system_prompt_lists_vision_gui_and_forbids_credentials():
    prompt = AgentExecutor.SYSTEM_PROMPT
    for name in ("capture_and_inspect_screen", *GUI_TOOLS):
        assert name in prompt
    assert "NEVER type into password" in prompt and "approval" in prompt.lower()
