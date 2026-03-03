from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import io
from typing import Optional

import numpy as np

try:
    import easyocr
except ImportError:  # pragma: no cover - optional in some deployments
    easyocr = None

try:
    import pillow_heif
except ImportError:  # pragma: no cover - optional in some deployments
    pillow_heif = None

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:  # pragma: no cover - optional in some deployments
    Image = None

    class UnidentifiedImageError(Exception):
        pass

_READER = None
_HEIF_READY = False


@dataclass
class UploadMetadata:
    ocr_text: str
    taken_at: Optional[str]


def _init_heif() -> None:
    global _HEIF_READY
    if _HEIF_READY:
        return
    if pillow_heif is not None:
        pillow_heif.register_heif_opener()
    _HEIF_READY = True


def _reader():
    global _READER
    if _READER is not None:
        return _READER
    if easyocr is None:
        return None
    # Use CPU to avoid unexpected GPU/CUDA requirements in common deployments.
    _READER = easyocr.Reader(["en"], gpu=False)
    return _READER


def _extract_taken_at(image: "Image.Image") -> Optional[str]:
    try:
        exif = image.getexif()
    except Exception:
        return None
    raw = exif.get(36867) or exif.get(306)
    if raw is None:
        return None
    raw_text = str(raw).strip()
    if not raw_text:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw_text, fmt).isoformat()
        except ValueError:
            continue
    return None


def _extract_ocr_text(image: "Image.Image") -> str:
    reader = _reader()
    if reader is None:
        return ""
    if image.mode != "RGB":
        image = image.convert("RGB")
    image_array = np.array(image)
    try:
        tokens = reader.readtext(image_array, detail=0)
    except Exception:
        return ""
    cleaned = [str(item).strip() for item in tokens if str(item).strip()]
    return " ".join(cleaned).strip()


def extract_upload_metadata(image_bytes: bytes) -> UploadMetadata:
    if not image_bytes or Image is None:
        return UploadMetadata(ocr_text="", taken_at=None)
    _init_heif()
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            taken_at = _extract_taken_at(image)
            ocr_text = _extract_ocr_text(image)
            return UploadMetadata(ocr_text=ocr_text, taken_at=taken_at)
    except (UnidentifiedImageError, OSError):
        return UploadMetadata(ocr_text="", taken_at=None)
