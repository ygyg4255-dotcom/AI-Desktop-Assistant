"""Background camera service: RAM-only buffering, instant capture, comparison, clean shutdown."""
import base64
import io
import sys
import threading
import time
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from src.agent import tools as tools_module
from src.agent.control_intents import (
    CAMERA_START, CAMERA_STOP, ControlHandler, match_control_intent,
)
from src.agent.permissions import PermissionGate
from src.agent.tools import create_default_registry
from src.services.camera_service import CameraService, CameraServiceError

FAST = dict(fps=500, snapshot_interval=0.0)  # tests must not wait on real-time pacing


class FakeCap:
    """Cycles a different solid colour per frame so before/after frames are distinguishable."""

    def __init__(self, opened=True, fail=False):
        self._opened, self._fail = opened, fail
        self.released = False
        self.release_calls = 0
        self.reads = 0

    def isOpened(self):
        return self._opened

    def read(self):
        self.reads += 1
        if self._fail:
            return False, None
        return True, np.full((720, 1920, 3), self.reads % 250, np.uint8)

    def release(self):
        self.released = True
        self.release_calls += 1


@pytest.fixture
def fake_cv2(monkeypatch):
    cv2 = MagicMock()
    cv2.CAP_DSHOW = 700
    cv2.COLOR_BGR2RGB = 4
    cv2.cvtColor.side_effect = lambda f, code: f[:, :, ::-1].copy()
    cv2.resize.side_effect = lambda f, size: np.zeros((size[1], size[0], 3), np.uint8)
    cv2.cap = FakeCap()
    cv2.VideoCapture.side_effect = lambda *a, **k: cv2.cap
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    return cv2


@pytest.fixture
def service(fake_cv2):
    svc = CameraService(**FAST)
    yield svc
    svc.stop()


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


# ------------------------------------------------------------- start / stop
def test_start_runs_a_daemon_thread_and_fills_latest_frame(service, fake_cv2):
    assert service.start() is True
    assert service.is_running
    thread = next(t for t in threading.enumerate() if t.name == "AltronCameraService")
    assert thread.daemon
    assert service.wait_for_frame(2.0) and service.latest_frame is not None
    fake_cv2.VideoCapture.assert_called_once_with(0, 700)


def test_start_twice_does_not_open_a_second_camera(service, fake_cv2):
    service.start()
    assert service.start() is True
    assert fake_cv2.VideoCapture.call_count == 1


def test_stop_joins_thread_and_releases_camera(service, fake_cv2):
    service.start()
    service.wait_for_frame(2.0)
    service.stop()

    assert not service.is_running
    assert fake_cv2.cap.released
    assert fake_cv2.cap.release_calls == 1
    assert not any(t.name == "AltronCameraService" for t in threading.enumerate())
    assert service.latest_frame is None and service.snapshot_count == 0  # RAM dropped


def test_camera_released_even_if_read_raises(fake_cv2):
    cap = FakeCap()
    cap.read = MagicMock(side_effect=RuntimeError("driver crashed"))
    fake_cv2.cap = cap
    svc = CameraService(**FAST)
    svc.start()
    time.sleep(0.05)
    svc.stop()
    assert cap.released


def test_restart_after_stop_works(service, fake_cv2):
    service.start()
    service.stop()
    fake_cv2.cap = FakeCap()
    assert service.start() is True
    assert service.wait_for_frame(2.0)


# ------------------------------------------------------------ no camera / cv2
def test_no_camera_fails_gracefully(fake_cv2, caplog):
    fake_cv2.cap = FakeCap(opened=False)
    svc = CameraService(**FAST)
    with caplog.at_level("WARNING"):
        assert svc.start() is False
    assert not svc.is_running
    assert "No camera found" in svc.last_error
    assert fake_cv2.cap.released
    assert "Camera service not started" in caplog.text
    svc.stop()  # stopping a never-started service is harmless


def test_missing_cv2_fails_gracefully(monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)
    svc = CameraService(**FAST)
    assert svc.start() is False
    assert "opencv-python" in svc.last_error


def test_capture_when_not_running_raises_clear_error(fake_cv2):
    svc = CameraService(**FAST)
    with pytest.raises(CameraServiceError, match="not running"):
        svc.get_latest_jpeg()
    with pytest.raises(CameraServiceError, match="not running"):
        svc.get_before_after_jpegs()


def test_service_stops_itself_when_camera_stops_returning_frames(fake_cv2, monkeypatch):
    monkeypatch.setattr("src.services.camera_service.MAX_CONSECUTIVE_FAILURES", 5)
    fake_cv2.cap = FakeCap(fail=True)
    svc = CameraService(**FAST)
    svc.start()
    assert _wait_for(lambda: not svc.is_running)
    assert _wait_for(lambda: fake_cv2.cap.released)
    assert "stopped returning frames" in svc.last_error


# ------------------------------------------------------------ rolling buffer
def test_rolling_buffer_is_capped_and_ram_only(fake_cv2, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    svc = CameraService(fps=500, snapshot_interval=0.0, max_snapshots=8)
    svc.start()
    assert _wait_for(lambda: fake_cv2.cap.reads > 40)  # far more frames than the cap
    assert svc.snapshot_count == 8
    svc.stop()
    assert list(tmp_path.iterdir()) == []  # nothing was written to disk


def test_buffer_snapshots_are_downscaled_to_1280(fake_cv2):
    svc = CameraService(**FAST)
    svc.start()
    svc.wait_for_frame(2.0)
    assert _wait_for(lambda: svc.snapshot_count >= 1)
    svc.stop()
    fake_cv2.resize.assert_called()  # source frames are 1920 wide
    assert fake_cv2.resize.call_args[0][1][0] == 1280


def test_snapshot_interval_throttles_the_buffer(fake_cv2):
    clock = MagicMock(return_value=100.0)
    svc = CameraService(fps=500, snapshot_interval=2.0, clock=clock)
    svc.start()
    assert _wait_for(lambda: fake_cv2.cap.reads > 20)
    svc.stop()
    assert svc.snapshot_count == 0  # stop() clears; check the throttle via a live run below

    fake_cv2.cap = FakeCap()
    svc = CameraService(fps=500, snapshot_interval=2.0, clock=clock)
    svc.start()
    assert _wait_for(lambda: fake_cv2.cap.reads > 20)
    assert svc.snapshot_count == 1  # clock frozen: only the first frame was snapshotted
    svc.stop()


# ------------------------------------------------------------------ JPEGs
def _decode(jpeg: bytes) -> Image.Image:
    return Image.open(io.BytesIO(jpeg))


def test_latest_jpeg_is_resized_jpeg_from_ram(service):
    service.start()
    img = _decode(service.get_latest_jpeg())
    assert img.format == "JPEG" and img.width == 1280 and img.height == 480


def test_before_after_returns_earlier_and_current_frames(fake_cv2):
    now = [100.0]
    svc = CameraService(clock=lambda: now[0])
    release = threading.Event()
    svc._thread = threading.Thread(target=release.wait, daemon=True)  # stands in for the reader
    svc._thread.start()
    svc._cv2 = fake_cv2
    svc._frame_ready.set()
    old, mid, new = (np.full((10, 20, 3), v, np.uint8) for v in (11, 99, 200))
    with svc._lock:
        svc._snapshots.extend([(90.0, old), (96.0, mid), (99.0, new)])
        svc._latest_frame = new
    try:
        pair = svc.get_before_after_jpegs(seconds_ago=5)  # closest to t=95 is the t=96 snapshot
        assert pair.seconds_apart == pytest.approx(4.0)
        assert abs(int(np.asarray(_decode(pair.before)).mean()) - 99) < 8
        assert abs(int(np.asarray(_decode(pair.after)).mean()) - 200) < 8

        far = svc.get_before_after_jpegs(seconds_ago=60)  # older than the buffer: oldest snapshot
        assert abs(int(np.asarray(_decode(far.before)).mean()) - 11) < 8
    finally:
        release.set()
        svc._thread.join(timeout=2)


def test_before_after_without_history_raises(fake_cv2):
    svc = CameraService(clock=lambda: 1.0)
    release = threading.Event()
    svc._thread = threading.Thread(target=release.wait, daemon=True)
    svc._thread.start()
    svc._cv2 = fake_cv2
    svc._frame_ready.set()
    try:
        with pytest.raises(CameraServiceError, match="no history"):
            svc.get_before_after_jpegs()
    finally:
        release.set()
        svc._thread.join(timeout=2)


# ------------------------------------------------------- capture_camera tool
@pytest.fixture
def tool_service(fake_cv2, monkeypatch):
    svc = CameraService(**FAST)
    monkeypatch.setattr(tools_module, "get_camera_service", lambda: svc)
    yield svc
    svc.stop()


def test_capture_camera_uses_ram_when_service_running(tool_service, fake_cv2):
    tool_service.start()
    tool_service.wait_for_frame(2.0)
    opens_before = fake_cv2.VideoCapture.call_count

    result = create_default_registry().get("capture_camera").run({})

    assert result.success and result.image_kind == "camera"
    assert _decode_block(result.image).format == "JPEG"
    assert fake_cv2.VideoCapture.call_count == opens_before  # no second camera open


def test_capture_camera_falls_back_to_single_shot_when_stopped(tool_service, fake_cv2):
    fake_cv2.cap = FakeCap()
    result = create_default_registry().get("capture_camera").run({})
    assert result.success and fake_cv2.VideoCapture.call_count == 1
    assert fake_cv2.cap.released  # single-shot releases right away


def test_capture_camera_compare_attaches_two_labelled_images(tool_service):
    tool_service.start()
    assert _wait_for(lambda: tool_service.snapshot_count >= 1)

    result = create_default_registry().get("capture_camera").run({"compare_seconds_ago": 5})

    assert result.success
    blocks = [result.image, *result.extra_content]
    assert [b["type"] for b in blocks] == ["image", "text", "image"]
    assert "seconds ago" in blocks[1]["text"]
    assert result.output["seconds_apart"] >= 0


def test_compare_requires_background_camera(tool_service):
    result = create_default_registry().get("capture_camera").run({"compare_seconds_ago": 5})
    assert not result.success and "start camera" in result.error


@pytest.mark.parametrize("bad", ["abc", -3, 500])
def test_compare_rejects_invalid_seconds(tool_service, bad):
    result = create_default_registry().get("capture_camera").run({"compare_seconds_ago": bad})
    assert not result.success and "compare_seconds_ago" in result.error


def test_capture_camera_when_service_has_no_frame_reports_error(tool_service, monkeypatch):
    tool_service.start()
    monkeypatch.setattr(tool_service, "wait_for_frame", lambda timeout=2.0: False)
    result = create_default_registry().get("capture_camera").run({})
    assert not result.success and "not produced a frame" in result.error


# ----------------------------------------------------- start / stop controls
def test_camera_tools_are_safe_no_approval(tool_service):
    gate = PermissionGate()
    registry = create_default_registry()
    for name in ("camera_start", "camera_stop", "capture_camera"):
        assert registry.get(name) is not None and not registry.get(name).requires_permission
        assert gate.is_safe(name)


def test_camera_start_and_stop_tools(tool_service, fake_cv2):
    registry = create_default_registry()
    assert registry.get("camera_start").run({}).success
    assert tool_service.is_running
    assert registry.get("camera_stop").run({}).success
    assert not tool_service.is_running and fake_cv2.cap.released


def test_camera_start_tool_reports_missing_camera(tool_service, fake_cv2):
    fake_cv2.cap = FakeCap(opened=False)
    result = create_default_registry().get("camera_start").run({})
    assert not result.success and "No camera found" in result.error


@pytest.mark.parametrize("phrase,expected", [
    ("start camera", CAMERA_START), ("Altron, turn on the camera please", CAMERA_START),
    ("stop camera", CAMERA_STOP), ("turn off the webcam", CAMERA_STOP),
    ("can you start the camera and tell me what you see", None),  # strict whole-utterance match
    ("the camera is on the desk", None),
])
def test_camera_control_phrases(phrase, expected):
    assert match_control_intent(phrase) == expected


def test_control_handler_toggles_camera():
    calls = []
    handler = ControlHandler(
        lambda: None, lambda: None, lambda: None, lambda: None,
        start_camera=lambda: calls.append("start") or True,
        stop_camera=lambda: calls.append("stop"),
    )
    assert "Camera on" in handler.handle("start camera")
    assert "Camera off" in handler.handle("stop camera")
    assert calls == ["start", "stop"]


def test_control_handler_reports_camera_start_failure():
    handler = ControlHandler(
        lambda: None, lambda: None, lambda: None, lambda: None,
        start_camera=lambda: False, stop_camera=lambda: None,
    )
    assert "couldn't start the camera" in handler.handle("start camera")


# ------------------------------------------------- settings / privacy / main
@pytest.mark.parametrize("raw,expected", [
    (None, True), ("true", True), ("false", False), ("0", False), ("off", False), ("yes", True),
])
def test_camera_autostart_setting(tmp_path, monkeypatch, raw, expected):
    from src.config.settings import Settings
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ALTRON_CAMERA_AUTOSTART", raising=False)
    if raw is not None:
        monkeypatch.setenv("ALTRON_CAMERA_AUTOSTART", raw)
    env_file = tmp_path / ".env"  # an empty .env, never the real one
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("src.config.settings.resolve_env_path", lambda *a, **k: env_file)
    assert Settings().camera_autostart is expected


def test_main_stops_camera_on_shutdown():
    source = open("main.py", encoding="utf-8").read()
    assert "camera_service.stop()" in source
    assert source.index("tts_manager.stop()") < source.rindex("camera_service.stop()") < source.index("stop_hud_server(grace")


def test_camera_frames_are_transient_in_tool_results(tool_service):
    tool_service.start()
    tool_service.wait_for_frame(2.0)
    result = create_default_registry().get("capture_camera").run({"compare_seconds_ago": 5})
    b64 = result.image["source"]["data"]
    assert b64 not in repr(result) and b64 not in str(result.output)  # never in output/repr/logs


def _decode_block(block) -> Image.Image:
    return _decode(base64.b64decode(block["source"]["data"]))
