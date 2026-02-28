import os
import time
from typing import Any, Dict, List
from pathlib import Path
import sys

import numpy as np
import cv2
# import pytesseract
from PIL import Image, ImageFile
from PIL.ExifTags import TAGS, GPSTAGS
import pillow_heif
from geopy.geocoders import Nominatim # <--- New library
from transformers import AutoModelForCausalLM
import torch
import time
from scripts.database_helper import *

import easyocr
import numpy as np
import math

# Setup
pillow_heif.register_heif_opener()
geolocator = Nominatim(user_agent="TagLens", domain="localhost:8080", scheme="http")
if torch and torch.cuda.is_available():
    device = "cuda"
elif torch and getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
    device = "mps"
else:
    device = "cpu"

print(f"Using device: {device}")

# if os.name == "nt":
#     _tess_default = r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
#     if os.path.exists(_tess_default):
#         pytesseract.pytesseract.tesseract_cmd = _tess_default

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

reader = easyocr.Reader(['en'], gpu=True)
ImageFile.LOAD_TRUNCATED_IMAGES = True


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


# def extract_ocr(img: Image.Image) -> Dict[str, Any]:
#     """Extract OCR text and bounding boxes from image
#     Returns dict with keys: text (str), boxes (List[Dict]), raw (dict), available (bool).
#     """
#     result: Dict[str, Any] = {"text": "", "boxes": [], "raw": {}, "available": True}
#     try:
#         data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config='--psm 11')
#         test = pytesseract.image_to_string(img, config='--psm 11')
#         result["raw"] = data
#         print(result)
#         print(test)
#         lines: List[str] = []
#         boxes: List[Dict[str, int]] = []
#         n = len(data.get("text", []))
#         for i in range(n):
#             conf = int(data.get("conf", ["-1"])[i]) if data.get("conf") else -1
#             txt = data.get("text", [""])[i]
#             if txt and conf >= 60:  # filter low-confidence tokens
#                 lines.append(txt)
#                 boxes.append(
#                     {
#                         "x": int(data.get("left", [0])[i]),
#                         "y": int(data.get("top", [0])[i]),
#                         "w": int(data.get("width", [0])[i]),
#                         "h": int(data.get("height", [0])[i]),
#                         "conf": conf,
#                     }
#                 )
#         result["text"] = " ".join(lines).strip()
#         result["boxes"] = boxes
#     except (pytesseract.TesseractNotFoundError, OSError) as e:
#         # Tesseract binary not found on system; mark unavailable gracefully
#         result["available"] = False
#         result["text"] = ""
#         result["boxes"] = []
#         result["raw"] = {"error": str(e)}
#         print(e)
#     return result

def ocr2(img):
    result = None
        
    # 3. Strip away any weird transparency layers (forces standard RGB)
    if img.mode != 'RGB':
        img = img.convert('RGB')

    try:
        # 2. Read the text
        img_array = np.array(img)
        
        text_list = reader.readtext(img_array, detail=0)
        result = " ".join(text_list).strip()
        
    except Exception as e:
        print(f"OCR error {e}")
    return result

# Helper func
def to_deci(val):
    return float(val[0]) + (float(val[1]) / 60.0) + (float(val[2]) / 3600.0)

def get_complete_metadata(path, conn, user_id):
    # Open the image and time how long that takes
    start = time.perf_counter()
    img = Image.open(path)
    end = time.perf_counter()
    open_time = end - start
    
    # Get basic metadata like size and whatnot
    start = time.perf_counter()
    file_size_mb = os.path.getsize(path) / (1024 * 1024)
    w, h = img.size
    mp = (w * h) / 1_000_000
    exif = img.getexif()
    exif_ifd = exif.get_ifd(34665)  # Technical
    gps_info = exif.get_ifd(34853)  # GPS 

    phone_make = exif.get(271, "Unknown")
    phone_model = exif.get(272, "Unknown")
    date = exif.get(36867) or exif.get(306) or None

    # This is camera info
    iso = exif_ifd.get(34855, None)
    f_stop = exif_ifd.get(33437, None)
    focal = exif_ifd.get(37386, None)
    exposure = exif_ifd.get(33434, None)
    shutter = f"1/{int(1/exposure)}" if (isinstance(exposure, (int, float)) and exposure < 1) else f"{exposure}"
    
    end = time.perf_counter()
    non_gps_time = end -start

    # This is to get location data
    location_data = None
    lat_lon_str = "N/A"
    lat = None
    lon = None

    # print(gps_info)
    if gps_info and len(gps_info) > 1:
        raw_gps = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}

        lat = raw_gps.get("GPSLatitude")
        lon = raw_gps.get("GPSLongitude")
        try:
            if lat and lon:
                lat = to_deci(lat)
                lon = to_deci(lon)

                if raw_gps.get("GPSLatitudeRef") == "S": lat = -lat
                if raw_gps.get("GPSLongitudeRef") == "W": lon = -lon
                
                lat_lon_str = f"{lat}, {lon}"
                try:
                    # This reaches out to OpenStreetMap to get the name
                    location_data = geolocator.reverse((lat, lon), language='en')
                except Exception as e:
                    location_data = None
                    print(f"Location Error: {e}")
        except ZeroDivisionError:
            lat = None
            lon = None

    end = time.perf_counter()
    gps_time = end-start

    # # Face recognition (commented out for now)
    # start = time.perf_counter()
    # faces = detect_faces(img)
    # end = time.perf_counter()
    # face_time = end - start



    # Captioning
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
            print(f"Captioning Error: {e}")
            output_caption = None
        end = time.perf_counter()
        caption_time = end-start
    
    # OCR
    start = time.perf_counter()
    ocr = ocr2(img)
    end = time.perf_counter()
    ocr_time = end - start

    # --- FINAL OUTPUT ---
    # print(f"\n{'='*40}")
    # print(f"FILE:        {os.path.basename(path)}")
    # print(f"SIZE:        {file_size_mb:.2f} MB")
    # print(f"QUALITY:     {w}x{h} ({mp:.2f}MP)")
    # print(f"DEVICE:      {phone_make} {phone_model}")
    # print(f"DATE:        {date}")
    # print(f"SETTINGS:    ƒ/{f_stop} | {shutter}s | {focal}mm | ISO {iso}")
    # print(f"COORDS:      {lat_lon_str}")
    # print(f"LOCATION:    {location_data}")
    # if output_caption is not None:
    #     print(f"CAPTION:     {output_caption}")
    # Faces summary
    # print(f"FACES:       {len(faces)} detected")
    # if faces:
    #     print("FACE BOXES:  " + ", ".join([f"(x={f['x']}, y={f['y']}, w={f['w']}, h={f['h']})" for f in faces]))
    
    # OCR summary
    # ocr_status = "available" if ocr.get("available", True) else "not available"
    # print(f"OCR:         {ocr_status}; {len(ocr.get('boxes', []))} tokens")
    # print(ocr)
    # if ocr.get("text"):
    #     preview = (ocr["text"][:180] + "...") if len(ocr["text"]) > 180 else ocr["text"]
    # print(f"OCR TEXT:    {ocr}")
    # print(f"{'='*40}\n")


    # print(f"Time to open: {open_time}")
    # print(f"Time to get not gps data: {non_gps_time}")
    # print(f"Time to get full data: {gps_time}")
    # # print(f"Time to detect faces: {face_time}")
    # print(f"Time to extract OCR: {ocr_time}")
    # print(f"Time to get caption: {caption_time}")

    
    metadata = dict()
    metadata['filename'] = os.path.basename(path)
    metadata['filepath'] = path
    metadata['size'] = file_size_mb
    metadata['w'] = w
    metadata['h'] = h
    metadata['make'] = phone_make
    metadata['model'] = phone_model
    metadata['date'] = date
    metadata['iso'] = int(iso) if iso else None 
    metadata['f_stop'] = float(f_stop) if f_stop else None
    metadata['shutter'] = str(shutter) if shutter else None
    metadata['focal'] = float(focal) if focal else None
    metadata['lat'] = lat
    metadata['lon'] = lon
    # CHANGE THIS LATER
    metadata['loc_description'] = None
    metadata['loc_city'] = None
    metadata['loc_state'] = None
    metadata['loc_country'] = None
    if location_data:
        loc = str(location_data).split(', ')
        if len(loc) >= 3:
            metadata['loc_description'] = " ".join(loc[:-3])
            metadata['loc_city'] = loc[-3]
            metadata['loc_state'] = loc[-2]
            metadata['loc_country'] = loc[-1]
 
    metadata['caption'] = output_caption['caption']
    metadata['ocr'] = ocr

    metadata["user_id"] = user_id

    # # results to the SQLite db
    # try:
    #     db = Database()
    #     asyncio.run(db.initialize())
    #     faces_json = json.dumps(faces)
    #     filename = os.path.basename(path)
    #     saved = asyncio.run(db.create_image_metadata(filename, faces_json, ocr.get("text", "")))
    #     print(f"Saved image metadata to DB: id={saved.id} file={saved.filename}")
    # except Exception as e:
    #     print(f"Warning: failed to save image metadata: {e}")

    # print(metadata)
    save_photo_to_db(conn, metadata)

def handle_video(path, conn, user_id):
    start = time.perf_counter()
    
    # 1. Get basic file metadata
    file_size_mb = os.path.getsize(path) / (1024 * 1024)
    filename = os.path.basename(path)
    
    # 2. Package the metadata dictionary
    # Keeping this strictly to the basics per your DB requirements:
    # user_id, filepath, size, and filename (for B2 upload extension).
    metadata = {
        'filename': filename,
        'filepath': path,
        'size': file_size_mb,
        'user_id': user_id
    }
    
    end = time.perf_counter()
    process_time = end - start
    
    # --- FINAL OUTPUT ---
    # print(f"\n{'='*40}")
    # print(f"FILE:        {filename}")
    # print(f"SIZE:        {file_size_mb:.2f} MB")
    # print(f"TYPE:        Video (Minimal Metadata)")
    # print(f"{'='*40}\n")
    # print(f"Time to process video: {process_time:.5f}s")
    
    # 3. Hand off to the SQLite DB
    save_video_to_db(conn, metadata)

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else r'test_images\IMG_8700.heif'
    curr = init_db()
    if not os.path.exists(target):
        print(f"Input image not found: {target}")
    else:
        get_complete_metadata(r"D:\Photos\takeout-20260212T234250Z-3-001\Takeout\Google Photos\Photos from 2020\IMG_3301.JPG", curr, 1)
        get_complete_metadata(r"D:\Photos\takeout-20260212T234250Z-3-001\Takeout\Google Photos\Photos from 2021\IMG_1598.WEBP", curr, 1)
        get_complete_metadata(target, curr, 1)
        # download_from_b2("1/1.heif", "test_images\\pain.heif")
