from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
from dataclasses import dataclass
from urllib import error as urllib_error, request as urllib_request
import importlib.util
from typing import Any, Dict, List, Optional, Tuple

# Keep imports lightweight; heavy deps are loaded lazily in helpers.
try:  # Pillow is optional in the wider app but required for metadata extraction.
    from PIL import Image, ImageFile
    from PIL.ExifTags import GPSTAGS
except Exception:  # pragma: no cover - optional dependency
    Image = None
    ImageFile = None
    GPSTAGS = None

if ImageFile:
    ImageFile.LOAD_TRUNCATED_IMAGES = True


_OPTIONAL_DEP_SPECS = {
    "pillow": "PIL",
    "opencv-python": "cv2",
    "numpy": "numpy",
    "easyocr": "easyocr",
    "torch": "torch",
    "geopy": "geopy",
    "pillow-heif": "pillow_heif",
}


def dependency_report() -> Dict[str, bool]:
    """Return availability for optional metadata dependencies."""
    report: Dict[str, bool] = {}
    for package, module in _OPTIONAL_DEP_SPECS.items():
        report[package] = importlib.util.find_spec(module) is not None
    return report


def missing_dependencies() -> List[str]:
    """Return a list of missing optional dependencies."""
    return [name for name, present in dependency_report().items() if not present]


@dataclass
class MetadataResult:
    faces: List[Dict[str, int]]
    ocr_text: str
    caption: str
    lat: Optional[float]
    lon: Optional[float]
    loc_description: Optional[str]
    loc_city: Optional[str]
    loc_state: Optional[str]
    loc_country: Optional[str]
    make: Optional[str]
    model: Optional[str]
    iso: Optional[int]
    f_stop: Optional[float]
    shutter_speed: Optional[str]
    focal_length: Optional[float]
    width: Optional[int]
    height: Optional[int]
    file_size_mb: Optional[float]
    taken_at: Optional[str]


_reader_lock = threading.Lock()
_easyocr_reader = None

_cascade_lock = threading.Lock()
_face_cascade = None


def _torch_available() -> bool:
    try:
        import torch  # noqa: F401
    except Exception:
        return False
    return True


def _torch_device() -> str:
    try:
        import torch
    except Exception:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _ollama_endpoint() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")


def _ollama_model() -> str:
    return os.getenv("OLLAMA_MODEL", "qwen3.5:4b")


def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is not None:
        return _easyocr_reader
    try:
        import easyocr
    except Exception:
        return None
    gpu = False
    if _torch_available():
        try:
            import torch

            gpu = torch.cuda.is_available()
        except Exception:
            gpu = False
    with _reader_lock:
        if _easyocr_reader is not None:
            return _easyocr_reader
        try:
            _easyocr_reader = easyocr.Reader(["en"], gpu=gpu)
        except Exception:
            _easyocr_reader = None
    return _easyocr_reader


def _optional_cv2():
    try:
        import cv2
    except Exception:
        return None
    return cv2


def _optional_numpy():
    try:
        import numpy as np
    except Exception:
        return None
    return np


def _optional_geopy():
    try:
        from geopy.geocoders import Nominatim
    except Exception:
        return None
    return Nominatim


def _optional_heif():
    try:
        import pillow_heif
    except Exception:
        return None
    return pillow_heif


def _register_heif() -> None:
    heif = _optional_heif()
    if heif:
        try:
            heif.register_heif_opener()
        except Exception:
            return


def _to_deci(val: Any) -> Optional[float]:
    try:
        return float(val[0]) + (float(val[1]) / 60.0) + (float(val[2]) / 3600.0)
    except Exception:
        return None


def _extract_gps(exif: Any) -> Tuple[Optional[float], Optional[float]]:
    if not GPSTAGS or not exif:
        return None, None
    gps_info = exif.get_ifd(34853) if hasattr(exif, "get_ifd") else None
    if not gps_info:
        return None, None
    raw_gps = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}
    lat = raw_gps.get("GPSLatitude")
    lon = raw_gps.get("GPSLongitude")
    lat_val = _to_deci(lat) if lat else None
    lon_val = _to_deci(lon) if lon else None
    if lat_val is None or lon_val is None:
        return None, None
    if raw_gps.get("GPSLatitudeRef") == "S":
        lat_val = -lat_val
    if raw_gps.get("GPSLongitudeRef") == "W":
        lon_val = -lon_val
    return lat_val, lon_val


def _reverse_geocode(lat: float, lon: float) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    Nominatim = _optional_geopy()
    if not Nominatim:
        return None, None, None, None
    try:
        geolocator = Nominatim(user_agent="TagLens")
        location_data = geolocator.reverse((lat, lon), language="en")
    except Exception:
        return None, None, None, None
    if not location_data:
        return None, None, None, None
    loc = str(location_data).split(", ")
    if len(loc) < 3:
        return None, None, None, None
    return " ".join(loc[:-3]), loc[-3], loc[-2], loc[-1]


def _get_face_cascade():
    global _face_cascade
    if _face_cascade is not None:
        return _face_cascade
    cv2 = _optional_cv2()
    if not cv2:
        return None
    with _cascade_lock:
        if _face_cascade is not None:
            return _face_cascade
        try:
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            _face_cascade = cv2.CascadeClassifier(cascade_path)
        except Exception:
            _face_cascade = None
    return _face_cascade


def _extract_faces(img: "Image.Image") -> List[Dict[str, int]]:
    face_cascade = _get_face_cascade()
    np = _optional_numpy()
    if not face_cascade or not np:
        return []
    cv2 = _optional_cv2()
    if not cv2:
        return []
    try:
        rgb = np.array(img)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        faces = face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
        )
        return [
            {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
            for (x, y, w, h) in faces
        ]
    except Exception:
        return []


def _extract_ocr(img: "Image.Image") -> str:
    reader = _get_easyocr_reader()
    if reader is None:
        return ""
    np = _optional_numpy()
    if not np:
        return ""
    try:
        if img.mode != "RGB":
            img = img.convert("RGB")
        img_array = np.array(img)
        text_list = reader.readtext(img_array, detail=0)
        return " ".join(text_list).strip()
    except Exception:
        return ""


def _extract_caption(img: "Image.Image") -> str:
    buf = io.BytesIO()
    try:
        rgb = img.convert("RGB") if img.mode != "RGB" else img
        rgb.save(buf, format="JPEG")
    except Exception:
        return ""
    img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    endpoint = f"{_ollama_endpoint()}/api/generate"
    payload = {
        "model": _ollama_model(),
        "prompt": (
            "You are describing a photo for search and organization. "
            "Write 1-2 concise sentences with the key visible subjects, setting, and notable details."
        ),
        "stream": False,
        "images": [img_b64],
    }
    req = urllib_request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    return (parsed.get("response") or "").strip()


def _extract_exif(img: "Image.Image") -> Dict[str, Any]:
    exif = img.getexif() if hasattr(img, "getexif") else None
    if not exif:
        return {
            "make": None,
            "model": None,
            "date": None,
            "iso": None,
            "f_stop": None,
            "shutter_speed": None,
            "focal_length": None,
            "lat": None,
            "lon": None,
        }
    exif_ifd = exif.get_ifd(34665) if hasattr(exif, "get_ifd") else {}
    make = exif.get(271)
    model = exif.get(272)
    date = exif.get(36867) or exif.get(306)
    iso = exif_ifd.get(34855)
    f_stop = exif_ifd.get(33437)
    focal = exif_ifd.get(37386)
    exposure = exif_ifd.get(33434)
    shutter_speed = None
    if isinstance(exposure, (int, float)) and exposure > 0 and exposure < 1:
        shutter_speed = f"1/{int(1/exposure)}"
    elif exposure is not None:
        shutter_speed = str(exposure)
    lat, lon = _extract_gps(exif)
    return {
        "make": str(make) if make is not None else None,
        "model": str(model) if model is not None else None,
        "date": str(date) if date is not None else None,
        "iso": int(iso) if iso else None,
        "f_stop": float(f_stop) if f_stop else None,
        "shutter_speed": shutter_speed,
        "focal_length": float(focal) if focal else None,
        "lat": lat,
        "lon": lon,
    }


def extract_metadata_from_image(
    img: "Image.Image",
    *,
    file_size_mb: Optional[float] = None,
) -> MetadataResult:
    from concurrent.futures import ThreadPoolExecutor

    exif = _extract_exif(img)
    lat = exif.get("lat")
    lon = exif.get("lon")

    with ThreadPoolExecutor(max_workers=4) as pool:
        face_fut = pool.submit(_extract_faces, img)
        ocr_fut = pool.submit(_extract_ocr, img)
        caption_fut = pool.submit(_extract_caption, img)
        geo_fut = pool.submit(_reverse_geocode, lat, lon) if lat is not None and lon is not None else None

        faces = face_fut.result()
        ocr_text = ocr_fut.result()
        caption = caption_fut.result()
        if geo_fut:
            loc_description, loc_city, loc_state, loc_country = geo_fut.result()
        else:
            loc_description = loc_city = loc_state = loc_country = None

    width, height = img.size if hasattr(img, "size") else (None, None)

    return MetadataResult(
        faces=faces,
        ocr_text=ocr_text,
        caption=caption,
        lat=lat,
        lon=lon,
        loc_description=loc_description,
        loc_city=loc_city,
        loc_state=loc_state,
        loc_country=loc_country,
        make=exif.get("make"),
        model=exif.get("model"),
        iso=exif.get("iso"),
        f_stop=exif.get("f_stop"),
        shutter_speed=exif.get("shutter_speed"),
        focal_length=exif.get("focal_length"),
        width=width,
        height=height,
        file_size_mb=file_size_mb,
        taken_at=exif.get("date"),
    )


def extract_metadata_from_bytes(
    image_bytes: bytes,
    *,
    filename: Optional[str] = None,
) -> Optional[MetadataResult]:
    if Image is None:
        return None
    if not image_bytes:
        return None
    _register_heif()
    try:
        img = Image.open(io.BytesIO(image_bytes))  # type: ignore[name-defined]
    except Exception:
        return None
    file_size_mb = len(image_bytes) / (1024 * 1024)
    return extract_metadata_from_image(img, file_size_mb=file_size_mb)


def extract_metadata_from_path(path: str) -> Optional[MetadataResult]:
    if Image is None:
        return None
    if not os.path.exists(path):
        return None
    _register_heif()
    try:
        img = Image.open(path)
    except Exception:
        return None
    file_size_mb = os.path.getsize(path) / (1024 * 1024)
    return extract_metadata_from_image(img, file_size_mb=file_size_mb)


def metadata_to_dict(result: MetadataResult) -> Dict[str, Any]:
    return {
        "faces": result.faces,
        "ocr_text": result.ocr_text,
        "caption": result.caption,
        "lat": result.lat,
        "lon": result.lon,
        "loc_description": result.loc_description,
        "loc_city": result.loc_city,
        "loc_state": result.loc_state,
        "loc_country": result.loc_country,
        "make": result.make,
        "model": result.model,
        "iso": result.iso,
        "f_stop": result.f_stop,
        "shutter_speed": result.shutter_speed,
        "focal_length": result.focal_length,
        "width": result.width,
        "height": result.height,
        "file_size_mb": result.file_size_mb,
        "taken_at": result.taken_at,
    }


def warmup() -> None:
    """Pre-initialize heavy models so the first request doesn't pay cold-start cost."""
    _get_easyocr_reader()
    _get_face_cascade()
    # Ping Ollama to verify reachability
    try:
        req = urllib_request.Request(
            f"{_ollama_endpoint()}/api/tags",
            method="GET",
        )
        with urllib_request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


def cleanup() -> None:
    """Release model memory."""
    global _easyocr_reader, _face_cascade
    _easyocr_reader = None
    _face_cascade = None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python metadata.py <image_path>")
        sys.exit(1)
    target = sys.argv[1]
    result = extract_metadata_from_path(target)
    if not result:
        print("Metadata extraction failed or dependencies missing.")
        sys.exit(2)
    print(json.dumps(metadata_to_dict(result), indent=2))
