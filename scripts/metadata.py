"""
Metadata extraction pipeline for images and videos.

Purpose:
    Provides face detection, OCR, EXIF, and caption helpers for local scripts.

Authorship (git history, mapped to real names):
    Daniel (l33tdaniel), Srihari (dimes130), Arnav (arnav-jain1), Chloe (n518t893)
"""

import os
from typing import Dict, List
import sys

import numpy as np
import cv2
from PIL import Image, ImageFile
from PIL.ExifTags import GPSTAGS
import pillow_heif
from geopy.geocoders import Nominatim
from transformers import AutoModelForCausalLM
import torch
from scripts.database_helper import init_db, save_photo_to_db, save_video_to_db

import easyocr

# Setup: register HEIF/HEIC support so Pillow can open iPhone photos.
pillow_heif.register_heif_opener()

# Reverse-geocoder: converts GPS coordinates to human-readable addresses via
# OpenStreetMap's Nominatim API. Uses the default public endpoint; a local
# Nominatim instance can be specified by setting the 'domain' parameter.
geolocator = Nominatim(user_agent="TagLens")

# Device selection: prefer CUDA (NVIDIA GPU), then MPS (Apple Silicon), then CPU.
if torch and torch.cuda.is_available():
    device = "cuda"
    reader = easyocr.Reader(["en"], gpu=True)
elif (
    torch and getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
):
    device = "mps"
    reader = easyocr.Reader(["en"], gpu=True)
else:
    reader = easyocr.Reader(["en"], gpu=False)
    device = "cpu"

print(f"Using device: {device}")
model = None
if AutoModelForCausalLM and torch:
    try:
        model = AutoModelForCausalLM.from_pretrained(
            "vikhyatk/moondream2",
            trust_remote_code=True,
            dtype=torch.bfloat16,
            device_map=device,
        )
    except Exception as e:
        print(f"Captioning model unavailable; skipping captions: {e}")

ImageFile.LOAD_TRUNCATED_IMAGES = True


def detect_faces(img: Image.Image) -> List[Dict[str, int]]:
    """Detect faces using OpenCV's Haar cascade and return bounding boxes.

    This is a lightweight, CPU-only face detector used as a fallback when
    InsightFace is unavailable. It does NOT identify who the face belongs to —
    it only locates face regions in the image.

    Returns:
        A list of dicts with keys: x, y, w, h (pixel coordinates of each face).
    """
    # Convert PIL image to grayscale numpy array for OpenCV's cascade classifier.
    rgb = np.array(img)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    # Load the pre-trained Haar cascade model bundled with OpenCV.
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    face_cascade = cv2.CascadeClassifier(cascade_path)

    # Tuned parameters: scaleFactor=1.1 and minNeighbors=5 reduce false positives
    # while still catching faces at various distances from the camera.
    faces = face_cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
    )
    return [
        {"x": int(x), "y": int(y), "w": int(w), "h": int(h)} for (x, y, w, h) in faces
    ]




def ocr2(img):
    """Extract visible text from an image using EasyOCR.

    Converts the image to RGB (required by EasyOCR), runs text detection,
    and returns all recognized text joined into a single string.
    """
    result = None

    # EasyOCR requires RGB input — strip alpha/transparency channels.
    if img.mode != "RGB":
        img = img.convert("RGB")

    try:
        img_array = np.array(img)
        # detail=0 returns plain strings instead of (bbox, text, confidence) tuples.
        text_list = reader.readtext(img_array, detail=0)
        result = " ".join(text_list).strip()

    except Exception as e:
        print(f"OCR error {e}")
    return result


def to_deci(val):
    """Convert EXIF GPS coordinates from degrees/minutes/seconds to decimal.

    EXIF stores GPS as a tuple of three rationals: (degrees, minutes, seconds).
    This converts them to a single decimal-degree float for use with geocoders.
    """
    return float(val[0]) + (float(val[1]) / 60.0) + (float(val[2]) / 3600.0)

def get_complete_metadata(path, conn, user_id):
    """Extract all metadata from an image and persist it to the database.

    This is the main pipeline entry-point for photos. It:
      1. Reads EXIF data (camera info, timestamps, GPS coordinates)
      2. Reverse-geocodes GPS coordinates to a human-readable location
      3. Generates an AI caption via the Moondream2 model
      4. Runs OCR to extract any visible text
      5. Saves everything to SQLite + uploads the original to B2
    """
    img = Image.open(path)

    # Basic file and image dimensions.
    file_size_mb = os.path.getsize(path) / (1024 * 1024)
    w, h = img.size

    # EXIF tag groups: root EXIF, IFD 34665 (camera technical), IFD 34853 (GPS).
    exif = img.getexif()
    exif_ifd = exif.get_ifd(34665)  # Camera settings (ISO, aperture, etc.)
    gps_info = exif.get_ifd(34853)  # GPS coordinates

    # EXIF tag 271 = Make (e.g., "Apple"), tag 272 = Model (e.g., "iPhone 15 Pro").
    phone_make = exif.get(271, "Unknown")
    phone_model = exif.get(272, "Unknown")
    # Tag 36867 = DateTimeOriginal (when photo was taken), fallback to tag 306 (DateTime).
    date = exif.get(36867) or exif.get(306) or None

    # Camera exposure settings from the EXIF IFD sub-directory.
    iso = exif_ifd.get(34855, None)       # ISO sensitivity (e.g., 100, 800)
    f_stop = exif_ifd.get(33437, None)    # Aperture f-number (e.g., 1.8)
    focal = exif_ifd.get(37386, None)     # Focal length in mm
    exposure = exif_ifd.get(33434, None)  # Exposure time in seconds

    # Convert fractional exposure (e.g., 0.005) to human-readable form (e.g., "1/200").
    shutter = (
        f"1/{int(1/exposure)}"
        if (isinstance(exposure, (int, float)) and exposure > 0 and exposure < 1)
        else f"{exposure}"
    )

    # --- GPS and reverse geocoding ---
    # Extract lat/lon from EXIF GPS tags and convert to human-readable location
    # using the Nominatim geocoder (OpenStreetMap).
    location_data = None
    lat = None
    lon = None

    if gps_info and len(gps_info) > 1:
        # Map numeric EXIF GPS tag IDs to human-readable names (e.g., "GPSLatitude").
        raw_gps = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}

        lat = raw_gps.get("GPSLatitude")
        lon = raw_gps.get("GPSLongitude")
        try:
            if lat and lon:
                lat = to_deci(lat)
                lon = to_deci(lon)

                if raw_gps.get("GPSLatitudeRef") == "S":
                    lat = -lat
                if raw_gps.get("GPSLongitudeRef") == "W":
                    lon = -lon

                try:
                    # Reverse-geocode via Nominatim (OpenStreetMap) to get a
                    # human-readable address string from the GPS coordinates.
                    location_data = geolocator.reverse((lat, lon), language="en")
                except Exception as e:
                    location_data = None
                    print(f"Location Error: {e}")
        except ZeroDivisionError:
            lat = None
            lon = None

    # --- AI captioning ---
    # Generate a short natural-language description of the image using Moondream2.
    output_caption = None
    if model:
        try:
            output_caption = model.caption(
                img,
                length="short",
            )
        except Exception as e:
            print(f"Captioning Error: {e}")
            output_caption = None

    # --- OCR: extract any visible text from the photo ---
    ocr = ocr2(img)

    # --- Assemble the final metadata dictionary for database insertion ---
    metadata = dict()
    metadata["filename"] = os.path.basename(path)
    metadata["filepath"] = path
    metadata["size"] = file_size_mb
    metadata["w"] = w
    metadata["h"] = h
    metadata["make"] = phone_make
    metadata["model"] = phone_model
    metadata["date"] = date
    metadata["iso"] = int(iso) if iso else None
    metadata["f_stop"] = float(f_stop) if f_stop else None
    metadata["shutter"] = str(shutter) if shutter else None
    metadata["focal"] = float(focal) if focal else None
    metadata["lat"] = lat
    metadata["lon"] = lon
    metadata["loc_description"] = None
    metadata["loc_city"] = None
    metadata["loc_state"] = None
    metadata["loc_country"] = None
    if location_data:
        loc = str(location_data).split(", ")
        if len(loc) >= 3:
            metadata["loc_description"] = " ".join(loc[:-3])
            metadata["loc_city"] = loc[-3]
            metadata["loc_state"] = loc[-2]
            metadata["loc_country"] = loc[-1]

    metadata["caption"] = output_caption.get("caption", "") if isinstance(output_caption, dict) else (output_caption or "")
    metadata["ocr"] = ocr

    metadata["user_id"] = user_id

    save_photo_to_db(conn, metadata)


def handle_video(path, conn, user_id):
    """Process a video file: extract basic metadata and persist to the database.

    Unlike photos, videos don't undergo EXIF extraction, captioning, or OCR.
    Only the file path, size, and user association are stored. The original
    video file is uploaded to B2 cloud storage via save_video_to_db().
    """
    file_size_mb = os.path.getsize(path) / (1024 * 1024)
    filename = os.path.basename(path)

    # Minimal metadata dict — videos only need path, size, and user info.
    metadata = {
        "filename": filename,
        "filepath": path,
        "size": file_size_mb,
        "user_id": user_id,
    }

    # Hand off to the SQLite DB
    save_video_to_db(conn, metadata)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python metadata.py <image_path>")
        sys.exit(1)
    target = sys.argv[1]
    curr = init_db()
    if not os.path.exists(target):
        print(f"Input image not found: {target}")
    else:
        get_complete_metadata(target, curr, 1)
