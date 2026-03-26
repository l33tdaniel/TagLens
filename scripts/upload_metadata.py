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
    from PIL.ExifTags import GPSTAGS
except ImportError:  # pragma: no cover - optional in some deployments
    Image = None

    class UnidentifiedImageError(Exception):
        pass

try: 
    from geopy.geocoders import Nominatim
    geolocator = Nominatim(user_agent="TagLens", scheme="http")
    # geolocator = Nominatim(user_agent="TagLens", domain="localhost:8080", scheme="http")
except ImportError:
    geolocator=None

_READER = None
_HEIF_READY = False


@dataclass
class UploadMetadata:
    ocr_text: str = ""
    taken_at: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    iso: Optional[int] = None
    f_stop: Optional[float] = None
    shutter: Optional[str] = None
    focal: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    loc_desc: Optional[str] = None
    loc_city: Optional[str] = None
    loc_state: Optional[str] = None
    loc_country: Optional[str] = None


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

def _camera_info(image):
    try:
        exif = image.getexif()
    except Exception:
        return "Unknown", "Unknown", None, None, None, None
    phone_make = exif.get(271, "Unknown")
    phone_model = exif.get(272, "Unknown")

    exif_ifd = exif.get_ifd(34665)  # Camera info

    iso = exif_ifd.get(34855, None)
    if iso:
        iso = int(iso)
    f_stop = exif_ifd.get(33437, None)
    if f_stop:
        f_stop = float(f_stop)
    focal = exif_ifd.get(37386, None)
    if focal:
        focal = int(focal)
    exposure = exif_ifd.get(33434, None)

    if exposure and int(exposure) != 1:
        shutter = (
            f"1/{int(1/exposure)}"
            if (isinstance(exposure, (int, float)) and exposure > 0 and exposure < 1)
            else f"{exposure}"
        )
    else:
        shutter = "None"
    
    return phone_make, phone_model, iso, f_stop, str(shutter), focal

# Helper func
def _to_deci(val):
    return float(val[0]) + (float(val[1]) / 60.0) + (float(val[2]) / 3600.0)

def _location_data(image):
    try:
        exif = image.getexif()
        gps_info = exif.get_ifd(34853)  # GPS

        location_data = None
        lat = None
        lon = None

        # This if statement extracts the info
        if gps_info and len(gps_info) > 1:
            raw_gps = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}

            lat = raw_gps.get("GPSLatitude")
            lon = raw_gps.get("GPSLongitude")
            if lat and lon:
                lat = _to_deci(lat)
                lon = _to_deci(lon)

                if raw_gps.get("GPSLatitudeRef") == "S":
                    lat = -lat
                if raw_gps.get("GPSLongitudeRef") == "W":
                    lon = -lon

                try:
                    location_data = geolocator.reverse((lat, lon), language="en")
                except:
                    location_data = None
        
        # This one processes the data
        if location_data:
            loc = str(location_data).split(", ")
            if len(loc) >= 3:
                loc_description = " ".join(loc[:-3])
                loc_city = loc[-3]
                loc_state = loc[-2]
                loc_country = loc[-1]
        else:
            loc_description = None
            loc_city = None
            loc_state = None
            loc_country = None
        if lat:
            float(lat)
        if lon:
            float(lon)
        return lat, lon, loc_description, loc_city, loc_state, loc_country
                

    except Exception:
            return None, None, None, None, None, None


def extract_upload_metadata(image_bytes: bytes) -> UploadMetadata:
    if not image_bytes or Image is None:
        return UploadMetadata(ocr_text="", taken_at=None)
    _init_heif()
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            taken_at = _extract_taken_at(image)
            # ocr_text = _extract_ocr_text_ollama(image)
            make, model, iso, f_stop, shutter, focal = _camera_info(image)
            lat, lon, desc, city, state, country = _location_data(image)
            
            return UploadMetadata(taken_at=taken_at,
                                  make=make, model=model, 
                                  iso=iso, f_stop=f_stop, shutter=shutter, focal=focal,
                                  lat=lat, lon=lon,
                                  loc_desc=desc, loc_city=city, loc_state=state, loc_country=country
                                  )
    except (UnidentifiedImageError, OSError):
        return UploadMetadata(taken_at=None,
                                make="Unknown", model="Unknown", 
                                iso=None, f_stop=None, shutter=None, focal=None,
                                lat=None, lon=None,
                                loc_desc=None, loc_city=None, loc_state=None, loc_country=None
                                )
