"""Screen vision helpers: compress screenshots and build multimodal image blocks.

Screenshots are resized/JPEG-compressed before being sent to the Claude Messages API
as base64 image blocks. Image data is transient: it may live in the in-memory message
list for the current turn, but must never reach ``conversation_history.json``,
``memory.json`` or the database. ``scrub_image_data`` / ``strip_image_data`` are the
choke points that replace it with a short text placeholder.
"""
import base64
import io
import re
from pathlib import Path
from typing import Any, Dict, List, Union

from src.config.log import get_logger

logger = get_logger(__name__)

MAX_WIDTH = 1280
JPEG_QUALITY = 75
SCREENSHOT_PLACEHOLDER = "[Screenshot captured]"
CAMERA_PLACEHOLDER = "[Camera photo captured]"

# Any long unbroken base64-looking run (a screenshot is tens of KB; prose never has this).
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{1000,}={0,2}")


def compress_image(img, max_width: int = MAX_WIDTH, quality: int = JPEG_QUALITY) -> bytes:
    """Resize ``img`` (a PIL image) to at most ``max_width`` wide and return JPEG bytes."""
    from PIL import Image

    img = img.convert("RGB")  # JPEG has no alpha channel
    if img.width > max_width:
        height = max(1, round(img.height * max_width / img.width))
        img = img.resize((max_width, height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def compress_image_file(path: Union[str, Path], max_width: int = MAX_WIDTH, quality: int = JPEG_QUALITY) -> bytes:
    from PIL import Image

    with Image.open(path) as img:
        return compress_image(img, max_width=max_width, quality=quality)


def image_block(jpeg_bytes: bytes) -> Dict[str, Any]:
    """Anthropic Messages API base64 image content block."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.b64encode(jpeg_bytes).decode("ascii"),
        },
    }


def is_image_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "image"


def strip_image_data(
    content: Union[str, List[Dict[str, Any]]], placeholder: str = SCREENSHOT_PLACEHOLDER
) -> Union[str, List[Dict[str, Any]]]:
    """Replace image blocks in a message content list with a text placeholder."""
    if not isinstance(content, list):
        return content
    return [
        {"type": "text", "text": placeholder} if is_image_block(b) else b
        for b in content
    ]


def scrub_image_data(text: str) -> str:
    """Remove any base64 blob from text bound for persistent storage."""
    if not isinstance(text, str):
        return text
    return _BASE64_BLOB.sub(SCREENSHOT_PLACEHOLDER, text)


class CameraError(RuntimeError):
    """The webcam could not be used (no cv2, no camera, busy, or no frame)."""


CAMERA_WARMUP_FRAMES = 5  # discard the first frames so auto-exposure can settle


def capture_camera_jpeg(index: int = 0, max_width: int = MAX_WIDTH, quality: int = JPEG_QUALITY) -> bytes:
    """Grab one frame from the default webcam and return it as compressed JPEG bytes.

    The camera is always released before returning (the LED turns off immediately),
    including when reading fails. Raises ``CameraError`` with a user-readable message.
    """
    try:
        import cv2
    except ImportError as e:
        raise CameraError("Camera support needs opencv-python (pip install opencv-python).") from e

    cap = None
    try:
        backend = getattr(cv2, "CAP_DSHOW", 0)  # DirectShow opens faster and releases cleanly on Windows
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            raise CameraError("No camera found, or it is in use by another app or blocked by Windows privacy settings.")
        frame = None
        for _ in range(CAMERA_WARMUP_FRAMES):
            ok, grabbed = cap.read()
            if ok and grabbed is not None:
                frame = grabbed
        if frame is None:
            raise CameraError("The camera opened but returned no image.")
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    except CameraError:
        raise
    except Exception as e:
        raise CameraError(f"Camera capture failed: {e}") from e
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception as e:  # never mask the real result
                logger.debug("Camera release failed: %s", e)

    from PIL import Image

    return compress_image(Image.fromarray(rgb), max_width=max_width, quality=quality)
