import os
import time
from typing import Any, Dict, List
from pathlib import Path
import sys

import numpy as np
import cv2
import pytesseract
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import pillow_heif
from geopy.geocoders import Nominatim # <--- New library
from transformers import AutoModelForCausalLM
import torch
import time

# Setup
pillow_heif.register_heif_opener()
geolocator = Nominatim(user_agent="TagLens")
if torch:
    if torch.cuda.is_available():
        device = "cuda"
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"
else:
    device = "cpu"

print(f"Using device: {device}")

if os.name == "nt":
    _tess_default = r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
    if os.path.exists(_tess_default):
        pytesseract.pytesseract.tesseract_cmd = _tess_default

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


def detect_faces(img: Image.Image) -> List[Dict[str, int]]:
    """Detect faces (no identity) and return bounding boxes.
    Returns a list of dicts with keys: x, y, w, h.
    """
    # Convert PIL image to grayscale numpy array for OpenCV
    rgb = np.array(img)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    face_cascade = cv2.CascadeClassifier(cascade_path)
    faces = face_cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
    )
    return [{"x": int(x), "y": int(y), "w": int(w), "h": int(h)} for (x, y, w, h) in faces]


def extract_ocr(img: Image.Image) -> Dict[str, Any]:
    """Extract OCR text and bounding boxes from image
    Returns dict with keys: text (str), boxes (List[Dict]), raw (dict), available (bool).
    """
    result: Dict[str, Any] = {"text": "", "boxes": [], "raw": {}, "available": True}
    try:
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        result["raw"] = data
        lines: List[str] = []
        boxes: List[Dict[str, int]] = []
        n = len(data.get("text", []))
        for i in range(n):
            conf = int(data.get("conf", ["-1"])[i]) if data.get("conf") else -1
            txt = data.get("text", [""])[i]
            if txt and conf >= 60:  # filter low-confidence tokens
                lines.append(txt)
                boxes.append(
                    {
                        "x": int(data.get("left", [0])[i]),
                        "y": int(data.get("top", [0])[i]),
                        "w": int(data.get("width", [0])[i]),
                        "h": int(data.get("height", [0])[i]),
                        "conf": conf,
                    }
                )
        result["text"] = " ".join(lines).strip()
        result["boxes"] = boxes
    except (pytesseract.TesseractNotFoundError, OSError) as e:
        # Tesseract binary not found on system; mark unavailable gracefully
        result["available"] = False
        result["text"] = ""
        result["boxes"] = []
        result["raw"] = {"error": str(e)}
    return result

def get_complete_metadata(path):
    start = time.perf_counter()
    img = Image.open(path)
    end = time.perf_counter()
    open_time = end - start
    start = time.perf_counter()
    file_size_mb = os.path.getsize(path) / (1024 * 1024)
    w, h = img.size
    mp = (w * h) / 1_000_000
    exif = img.getexif()
    exif_ifd = exif.get_ifd(34665)  # Technical
    gps_info = exif.get_ifd(34853)  # GPS 

    phone_make = exif.get(271, "Unknown")
    phone_model = exif.get(272, "Unknown")
    date = exif.get(36867) or exif.get(306) or "Unknown Date"

    iso = exif_ifd.get(34855, "N/A")
    f_stop = exif_ifd.get(33437, "N/A")
    focal = exif_ifd.get(37386, "N/A")
    exposure = exif_ifd.get(33434, "N/A")
    shutter = f"1/{int(1/exposure)}" if (isinstance(exposure, (int, float)) and exposure < 1) else f"{exposure}"
    location_data = None
    lat_lon_str = "N/A"
    end = time.perf_counter()
    non_gps_time = end -start
    if gps_info:
        def to_deci(val):
            return float(val[0]) + (float(val[1]) / 60.0) + (float(val[2]) / 3600.0)

        raw_gps = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}
        lat = to_deci(raw_gps.get("GPSLatitude"))
        lon = to_deci(raw_gps.get("GPSLongitude"))

        if raw_gps.get("GPSLatitudeRef") == "S": lat = -lat
        if raw_gps.get("GPSLongitudeRef") == "W": lon = -lon
        
        lat_lon_str = f"{lat}, {lon}"
        try:
            location_data = geolocator.reverse((lat, lon), language='en')
        except Exception as e:
            location_data = None
    end = time.perf_counter()
    metadata_time = end-start
    # timing
    start = time.perf_counter()
    faces = detect_faces(img)
    end = time.perf_counter()
    face_time = end - start
    # timing
    start = time.perf_counter()
    ocr = extract_ocr(img)
    end = time.perf_counter()
    ocr_time = end - start
    output_caption = None
    caption_time = 0.0
    if model:
        start = time.perf_counter()
        try:
            output_caption = model.caption(
                img,
                length="short",
            )
        except Exception as e:
            print(f"Captioning failed: {e}")
            output_caption = None
        end = time.perf_counter()
        caption_time = end-start
    # --- FINAL OUTPUT ---
    print(f"\n{'='*40}")
    print(f"FILE:        {os.path.basename(path)}")
    print(f"SIZE:        {file_size_mb:.2f} MB")
    print(f"QUALITY:     {w}x{h} ({mp:.2f}MP)")
    print(f"DEVICE:      {phone_make} {phone_model}")
    print(f"DATE:        {date}")
    print(f"SETTINGS:    ƒ/{f_stop} | {shutter}s | {focal}mm | ISO {iso}")
    print(f"COORDS:      {lat_lon_str}")
    print(f"LOCATION:    {location_data}")
    if output_caption is not None:
        print(f"CAPTION:     {output_caption}")
    # Faces summary
    print(f"FACES:       {len(faces)} detected")
    if faces:
        print("FACE BOXES:  " + ", ".join([f"(x={f['x']}, y={f['y']}, w={f['w']}, h={f['h']})" for f in faces]))
    # OCR summary
    ocr_status = "available" if ocr.get("available", True) else "not available"
    print(f"OCR:         {ocr_status}; {len(ocr.get('boxes', []))} tokens")
    if ocr.get("text"):
        preview = (ocr["text"][:180] + "...") if len(ocr["text"]) > 180 else ocr["text"]
        print(f"OCR TEXT:    {preview}")
    print(f"{'='*40}\n")


    print(f"Time to open: {open_time}")
    print(f"Time to get not gps data: {non_gps_time}")
    print(f"Time to get full data: {metadata_time}")
    print(f"Time to detect faces: {face_time}")
    print(f"Time to extract OCR: {ocr_time}")
    print(f"Time to get caption: {caption_time}")

    # results to the SQLite db
    try:
        db = Database()
        asyncio.run(db.initialize())
        faces_json = json.dumps(faces)
        filename = os.path.basename(path)
        saved = asyncio.run(db.create_image_metadata(filename, faces_json, ocr.get("text", "")))
        print(f"Saved image metadata to DB: id={saved.id} file={saved.filename}")
    except Exception as e:
        print(f"Warning: failed to save image metadata: {e}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else 'test_images/photo.HEIC'
    if not os.path.exists(target):
        print(f"Input image not found: {target}")
    else:
        get_complete_metadata(target)
