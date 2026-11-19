"""Background camera service: keeps the webcam warm in RAM for instant, zero-warmup capture.

Privacy and traffic guarantees:
- Frames live ONLY in memory (``latest_frame`` plus a small rolling buffer of downscaled
  snapshots). Nothing is ever written to disk.
- Nothing is ever sent to Claude by this module. A JPEG is produced only when a caller
  (the ``capture_camera`` tool, run because the user asked) requests it, so while idle
  there is zero API traffic.
- ``stop()`` joins the capture thread; the thread releases ``cv2.VideoCapture`` in a
  ``finally`` block, so the camera LED turns off.
"""
import collections
import threading
import time
from dataclasses import dataclass
from typing import Callable, Deque, Optional, Tuple

from src.config.log import get_logger

logger = get_logger(__name__)

DEFAULT_FPS = 8.0                # low, CPU-friendly read rate (~5-10 FPS)
SNAPSHOT_INTERVAL = 2.0          # one buffered snapshot every 2 seconds
MAX_SNAPSHOTS = 8                # 8 x 2 s = the last ~16 seconds
MAX_WIDTH = 1280
JPEG_QUALITY = 75
MAX_CONSECUTIVE_FAILURES = 50    # unplugged / revoked camera: give up instead of spinning
JOIN_TIMEOUT = 5.0


class CameraServiceError(RuntimeError):
    """The background camera cannot provide an image (not running, no frame yet, no history)."""


@dataclass(frozen=True)
class BeforeAfter:
    before: bytes
    after: bytes
    seconds_apart: float


def _frame_to_jpeg(frame, cv2_module, max_width: int = MAX_WIDTH, quality: int = JPEG_QUALITY) -> bytes:
    """Encode a BGR frame as JPEG (max 1280 px wide, quality 75) entirely in memory."""
    from src.agent.vision import compress_image
    from PIL import Image

    rgb = cv2_module.cvtColor(frame, cv2_module.COLOR_BGR2RGB)
    return compress_image(Image.fromarray(rgb), max_width=max_width, quality=quality)


def _downscale(frame, cv2_module, max_width: int = MAX_WIDTH):
    """Shrink a BGR frame to at most ``max_width`` so buffered snapshots stay small in RAM."""
    height, width = frame.shape[:2]
    if width <= max_width:
        return frame.copy()
    new_h = max(1, round(height * max_width / width))
    return cv2_module.resize(frame, (max_width, new_h))


class CameraService:
    """Thread-safe background webcam reader. See the module docstring for the guarantees."""

    def __init__(
        self,
        index: int = 0,
        fps: float = DEFAULT_FPS,
        snapshot_interval: float = SNAPSHOT_INTERVAL,
        max_snapshots: int = MAX_SNAPSHOTS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._index = index
        self._frame_delay = 1.0 / max(fps, 0.5)
        self._snapshot_interval = snapshot_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._frame_ready = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cv2 = None
        self._latest_frame = None
        self._snapshots: Deque[Tuple[float, object]] = collections.deque(maxlen=max_snapshots)
        self._last_error = ""

    # ------------------------------------------------------------------ state
    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop_event.is_set()

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def latest_frame(self):
        """The most recent frame (a copy), or None."""
        with self._lock:
            return None if self._latest_frame is None else self._latest_frame.copy()

    @property
    def snapshot_count(self) -> int:
        with self._lock:
            return len(self._snapshots)

    # -------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        """Open the camera and start the capture thread. Returns False (never raises) on failure."""
        if self.is_running:
            return True
        try:
            import cv2
        except ImportError:
            return self._fail("Camera service needs opencv-python (pip install opencv-python).")

        cap = None
        try:
            backend = getattr(cv2, "CAP_DSHOW", 0)  # DirectShow opens fast and releases cleanly on Windows
            cap = cv2.VideoCapture(self._index, backend)
            if not cap.isOpened():
                cap.release()
                return self._fail("No camera found, or it is in use by another app or blocked by Windows privacy settings.")
        except Exception as e:
            if cap is not None:
                try:
                    cap.release()
                except Exception as rel_err:
                    logger.debug("Camera release failed: %s", rel_err)
            return self._fail(f"Camera could not be opened: {e}")

        self._cv2 = cv2
        self._last_error = ""
        self._stop_event.clear()
        self._frame_ready.clear()
        with self._lock:
            self._latest_frame = None
            self._snapshots.clear()
        self._thread = threading.Thread(
            target=self._run, args=(cap,), name="AltronCameraService", daemon=True
        )
        self._thread.start()
        logger.info("Background camera started")
        return True

    def stop(self) -> None:
        """Stop the thread, release the camera and drop every buffered frame."""
        thread = self._thread
        self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=JOIN_TIMEOUT)
            if thread.is_alive():
                logger.warning("Camera thread did not stop within %.0fs", JOIN_TIMEOUT)
        self._thread = None
        with self._lock:
            self._latest_frame = None
            self._snapshots.clear()
        self._frame_ready.clear()
        logger.info("Background camera stopped")

    def _fail(self, message: str) -> bool:
        self._last_error = message
        logger.warning("Camera service not started: %s", message)
        return False

    def _run(self, cap) -> None:
        cv2 = self._cv2
        failures = 0
        last_snapshot = float("-inf")
        try:
            while not self._stop_event.is_set():
                try:
                    ok, frame = cap.read()
                except Exception as e:
                    ok, frame = False, None
                    logger.debug("Camera read raised: %s", e)
                if not ok or frame is None:
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        self._last_error = "The camera stopped returning frames."
                        logger.warning("Camera service stopping: %s", self._last_error)
                        self._stop_event.set()
                        break
                else:
                    failures = 0
                    now = self._clock()
                    with self._lock:
                        self._latest_frame = frame
                        if now - last_snapshot >= self._snapshot_interval:
                            self._snapshots.append((now, _downscale(frame, cv2)))
                            last_snapshot = now
                    self._frame_ready.set()
                self._stop_event.wait(self._frame_delay)
        finally:
            try:
                cap.release()
            except Exception as e:  # never mask the shutdown
                logger.debug("Camera release failed: %s", e)

    # ---------------------------------------------------------------- capture
    def wait_for_frame(self, timeout: float = 2.0) -> bool:
        return self._frame_ready.wait(timeout)

    def get_latest_jpeg(self, wait: float = 2.0) -> bytes:
        """Encode the newest frame from RAM. Raises CameraServiceError if the service is not running."""
        if not self.is_running:
            raise CameraServiceError("The background camera is not running.")
        if not self.wait_for_frame(wait):
            raise CameraServiceError("The background camera has not produced a frame yet.")
        frame = self.latest_frame
        if frame is None:
            raise CameraServiceError("The background camera has no frame.")
        return _frame_to_jpeg(frame, self._cv2)

    def get_before_after_jpegs(self, seconds_ago: float = 5.0, wait: float = 2.0) -> BeforeAfter:
        """The buffered snapshot closest to ``seconds_ago`` seconds back, plus the current frame."""
        if not self.is_running:
            raise CameraServiceError("The background camera is not running.")
        if not self.wait_for_frame(wait):
            raise CameraServiceError("The background camera has not produced a frame yet.")
        target = self._clock() - max(float(seconds_ago), 0.0)
        with self._lock:
            if not self._snapshots or self._latest_frame is None:
                raise CameraServiceError("The background camera has no history yet.")
            ts, before = min(self._snapshots, key=lambda item: abs(item[0] - target))
            after = self._latest_frame.copy()
            before = before.copy()
            apart = max(self._clock() - ts, 0.0)
        return BeforeAfter(
            before=_frame_to_jpeg(before, self._cv2),
            after=_frame_to_jpeg(after, self._cv2),
            seconds_apart=apart,
        )


_service: Optional[CameraService] = None
_service_lock = threading.Lock()


def get_camera_service() -> CameraService:
    """Process-wide camera service (created lazily, not started)."""
    global _service
    with _service_lock:
        if _service is None:
            _service = CameraService()
        return _service
