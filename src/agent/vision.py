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

MAX_WIDTH = 1280
JPEG_QUALITY = 75
SCREENSHOT_PLACEHOLDER = "[Screenshot captured]"

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


def strip_image_data(content: Union[str, List[Dict[str, Any]]]) -> Union[str, List[Dict[str, Any]]]:
    """Replace image blocks in a message content list with a text placeholder."""
    if not isinstance(content, list):
        return content
    return [
        {"type": "text", "text": SCREENSHOT_PLACEHOLDER} if is_image_block(b) else b
        for b in content
    ]


def scrub_image_data(text: str) -> str:
    """Remove any base64 blob from text bound for persistent storage."""
    if not isinstance(text, str):
        return text
    return _BASE64_BLOB.sub(SCREENSHOT_PLACEHOLDER, text)
