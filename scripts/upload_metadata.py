"""
Metadata extraction for uploaded images (used by the web upload endpoint).

Unlike scripts/metadata.py (which processes local files in batch), this module
operates on raw image bytes received from the browser. It extracts:
  - EXIF timestamps (taken_at) for chronological sorting
  - Camera info (make, model, ISO, aperture, shutter speed, focal length)
  - GPS coordinates + reverse-geocoded location (city, state, country)
  - OCR text (currently disabled in upload path, available for future use)

All heavy dependencies (easyocr, pillow_heif, PIL, geopy) are imported with
try/except guards so the app can still start in minimal deployments.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import io
from typing import Optional

import numpy as np

# --- Optional dependency imports ---
# Each ML/image library is wrapped in try/except so the server can start
# even if some libraries are missing (e.g., in lightweight test environments).

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
    # Reverse-geocoder: converts GPS coordinates to human-readable addresses
    # via OpenStreetMap's Nominatim API (public endpoint, no API key needed).
    geolocator = Nominatim(user_agent="TagLens")
except ImportError:
    geolocator=None

# Lazy-initialized singletons — created on first use to avoid startup cost.
_READER = None     # EasyOCR reader instance
_HEIF_READY = False  # Whether HEIF/HEIC opener has been registered with Pillow


@dataclass
class UploadMetadata:
    """Container for all metadata extracted from an uploaded image.

    Fields map directly to columns in the images table. All fields are optional
    except ocr_text (defaults to empty string) because not every image has EXIF
    data, GPS coordinates, or recognizable text.
    """
    ocr_text: str = ""                      # OCR-extracted text from the image
    taken_at: Optional[str] = None          # ISO-format timestamp from EXIF
    make: Optional[str] = None              # Camera manufacturer (e.g., "Apple")
    model: Optional[str] = None             # Camera model (e.g., "iPhone 15 Pro")
    iso: Optional[int] = None               # ISO sensitivity
    f_stop: Optional[float] = None          # Aperture f-number
    shutter: Optional[str] = None           # Shutter speed (e.g., "1/200")
    focal: Optional[float] = None           # Focal length in mm
    lat: Optional[float] = None             # GPS latitude (decimal degrees)
    lon: Optional[float] = None             # GPS longitude (decimal degrees)
    loc_desc: Optional[str] = None          # Street-level description
    loc_city: Optional[str] = None          # City name
    loc_state: Optional[str] = None         # State/province name
    loc_country: Optional[str] = None       # Country name


def _init_heif() -> None:
    """Register the HEIF/HEIC image opener with Pillow (one-time setup).

    iPhone photos are often in HEIC format. This call lets Pillow's Image.open()
    handle them transparently. Guarded by a flag to avoid redundant registration.
    """
    global _HEIF_READY
    if _HEIF_READY:
        return
    if pillow_heif is not None:
        pillow_heif.register_heif_opener()
    _HEIF_READY = True


def _reader():
    """Lazily initialize and return the EasyOCR reader singleton.

    Uses CPU-only mode to avoid requiring CUDA/GPU drivers on the server.
    Returns None if the easyocr package is not installed.
    """
    global _READER
    if _READER is not None:
        return _READER
    if easyocr is None:
        return None
    _READER = easyocr.Reader(["en"], gpu=False)
    return _READER


def _extract_taken_at(image: "Image.Image") -> Optional[str]:
    """Extract the photo's original timestamp from EXIF data.

    Checks EXIF tag 36867 (DateTimeOriginal) first, then falls back to
    tag 306 (DateTime). Parses both colon-separated and dash-separated
    date formats commonly found in camera EXIF data. Returns an ISO-format
    string (e.g., "2025-03-15T14:30:00") or None if no valid date is found.
    """
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
    # EXIF dates use colons (2025:03:15), while ISO uses dashes — handle both.
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw_text, fmt).isoformat()
        except ValueError:
            continue
    return None


def _extract_ocr_text(image: "Image.Image") -> str:
    """Run OCR on an image and return all detected text as a single string.

    Converts the image to RGB (required by EasyOCR), runs text detection with
    detail=0 (plain strings, no bounding boxes), and joins all tokens with spaces.
    Returns an empty string if EasyOCR is unavailable or if detection fails.
    """
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
    """Extract camera/phone make, model, and exposure settings from EXIF.

    Reads from the EXIF IFD sub-directory (tag group 34665) which contains
    technical camera settings. Returns a tuple of:
        (make, model, iso, f_stop, shutter_speed_str, focal_length)
    """
    try:
        exif = image.getexif()
    except Exception:
        return "Unknown", "Unknown", None, None, None, None

    # Tag 271 = Make (manufacturer), Tag 272 = Model (device name).
    phone_make = exif.get(271, "Unknown")
    phone_model = exif.get(272, "Unknown")

    # IFD 34665 contains the detailed camera exposure sub-tags.
    exif_ifd = exif.get_ifd(34665)

    iso = exif_ifd.get(34855, None)       # Tag 34855 = ISO speed
    if iso:
        iso = int(iso)
    f_stop = exif_ifd.get(33437, None)    # Tag 33437 = F-number (aperture)
    if f_stop:
        f_stop = float(f_stop)
    focal = exif_ifd.get(37386, None)     # Tag 37386 = Focal length in mm
    if focal:
        focal = int(focal)
    exposure = exif_ifd.get(33434, None)  # Tag 33434 = Exposure time in seconds

    # Convert fractional exposure (e.g., 0.005s) to readable form (e.g., "1/200").
    if exposure and int(exposure) != 1:
        shutter = (
            f"1/{int(1/exposure)}"
            if (isinstance(exposure, (int, float)) and exposure > 0 and exposure < 1)
            else f"{exposure}"
        )
    else:
        shutter = "None"

    return phone_make, phone_model, iso, f_stop, str(shutter), focal


def _to_deci(val):
    """Convert EXIF GPS degrees/minutes/seconds tuple to decimal degrees.

    EXIF stores GPS coordinates as three rationals: (degrees, minutes, seconds).
    Formula: decimal = degrees + (minutes / 60) + (seconds / 3600)
    """
    return float(val[0]) + (float(val[1]) / 60.0) + (float(val[2]) / 3600.0)


def _location_data(image):
    """Extract GPS coordinates from EXIF and reverse-geocode to a location.

    Reads GPS data from EXIF IFD 34853, converts DMS to decimal degrees,
    applies hemisphere corrections (S/W = negative), and then calls the
    Nominatim reverse geocoder to get a human-readable address.

    Returns:
        Tuple of (lat, lon, description, city, state, country).
        All None if GPS data is missing or geocoding fails.
    """
    try:
        exif = image.getexif()
        gps_info = exif.get_ifd(34853)  # EXIF GPS sub-directory

        location_data = None
        lat = None
        lon = None

        # Extract and convert GPS coordinates from EXIF tags.
        if gps_info and len(gps_info) > 1:
            # Map numeric EXIF GPS tag IDs to readable names (e.g., "GPSLatitude").
            raw_gps = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}

            lat = raw_gps.get("GPSLatitude")
            lon = raw_gps.get("GPSLongitude")
            if lat and lon:
                # Convert from degrees/minutes/seconds to decimal degrees.
                lat = _to_deci(lat)
                lon = _to_deci(lon)

                # Apply hemisphere sign: South latitudes and West longitudes are negative.
                if raw_gps.get("GPSLatitudeRef") == "S":
                    lat = -lat
                if raw_gps.get("GPSLongitudeRef") == "W":
                    lon = -lon

                # Reverse-geocode coordinates to get an address string.
                try:
                    location_data = geolocator.reverse((lat, lon), language="en")
                except:
                    location_data = None

        # Parse the Nominatim response string into structured location fields.
        # Nominatim returns a comma-separated string like:
        # "123 Main St, Springfield, Illinois, United States"
        # We split from the end: country, state, city, then everything else.
        if location_data:
            loc = str(location_data).split(", ")
            if len(loc) >= 3:
                loc_description = " ".join(loc[:-3])  # Street-level detail
                loc_city = loc[-3]
                loc_state = loc[-2]
                loc_country = loc[-1]
        else:
            loc_description = None
            loc_city = None
            loc_state = None
            loc_country = None

        # Ensure lat/lon are valid floats (guards against malformed EXIF).
        if lat:
            float(lat)
        if lon:
            float(lon)
        return lat, lon, loc_description, loc_city, loc_state, loc_country

    except Exception:
            return None, None, None, None, None, None


def extract_upload_metadata(image_bytes: bytes) -> UploadMetadata:
    """Main entry-point: extract all available metadata from raw image bytes.

    Called by the upload API handler after receiving a base64-decoded image from
    the browser. Opens the image in memory (supports JPEG, PNG, HEIC, WebP),
    extracts EXIF timestamps, camera info, GPS location, and returns a populated
    UploadMetadata dataclass. Gracefully degrades if the image is corrupt or if
    optional libraries are missing.
    """
    if not image_bytes or Image is None:
        return UploadMetadata(ocr_text="", taken_at=None)

    # Ensure HEIF/HEIC support is registered before opening the image.
    _init_heif()
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            taken_at = _extract_taken_at(image)
            make, model, iso, f_stop, shutter, focal = _camera_info(image)
            lat, lon, desc, city, state, country = _location_data(image)

            return UploadMetadata(taken_at=taken_at,
                                  make=make, model=model,
                                  iso=iso, f_stop=f_stop, shutter=shutter, focal=focal,
                                  lat=lat, lon=lon,
                                  loc_desc=desc, loc_city=city, loc_state=state, loc_country=country
                                  )
    except (UnidentifiedImageError, OSError):
        # Image is corrupt or in an unsupported format — return safe defaults.
        return UploadMetadata(taken_at=None,
                                make="Unknown", model="Unknown",
                                iso=None, f_stop=None, shutter=None, focal=None,
                                lat=None, lon=None,
                                loc_desc=None, loc_city=None, loc_state=None, loc_country=None
                                )
