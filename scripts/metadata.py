import os
import time
from typing import Any, Dict, List
import sys

import numpy as np
import cv2
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import pillow_heif
from geopy.geocoders import Nominatim
from transformers import AutoModelForCausalLM
import torch
from database_helper import *

import easyocr

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


def extract_ocr(path):
    """Extract OCR text and bounding boxes using easyocr.
    Returns text string and list of bounding boxes.
    """
    text = ""
    boxes = []
    try:
        # Read text with detailed bounding box info
        results = reader.readtext(path)
        text_list = []
        
        for detection in results:
            bbox, text_content, confidence = detection
            text_list.append(text_content)
            
            # Convert bbox (4 corner points) to x, y, w, h format
            x_coords = [point[0] for point in bbox]
            y_coords = [point[1] for point in bbox]
            x = min(x_coords)
            y = min(y_coords)
            w = max(x_coords) - x
            h = max(y_coords) - y
            
            boxes.append({
                "x": int(x),
                "y": int(y),
                "w": int(w),
                "h": int(h),
                "conf": float(confidence)
            })
        
        text = " ".join(text_list).strip()
        
    except Exception as e:
        print(f"OCR error: {e}")
    
    return text, boxes

def ocr2(path):
    result = None
    try:
        # 2. Read the text
        text_list = reader.readtext(path, detail=0)
        result = " ".join(text_list).strip()
        
    except Exception as e:
        print(f"OCR error {e}")
    return result

# Helper func
def to_deci(val):
    return float(val[0]) + (float(val[1]) / 60.0) + (float(val[2]) / 3600.0)

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

    if gps_info:
        raw_gps = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}
        lat = to_deci(raw_gps.get("GPSLatitude"))
        lon = to_deci(raw_gps.get("GPSLongitude"))

        if raw_gps.get("GPSLatitudeRef") == "S": lat = -lat
        if raw_gps.get("GPSLongitudeRef") == "W": lon = -lon
        
        lat_lon_str = f"{lat}, {lon}"
        try:
            # This reaches out to OpenStreetMap to get the name
            location_data = geolocator.reverse((lat, lon), language='en')
        except Exception as e:
            location_data = None
            print(e)
    end = time.perf_counter()
    gps_time = end-start

    # # Face recognition (commented out for now)
    # start = time.perf_counter()
    # faces = detect_faces(img)
    # end = time.perf_counter()
    # face_time = end - start


    # OCR
    start = time.perf_counter()
    ocr_text, ocr_boxes = extract_ocr(path)
    ocr = ocr2(path)
    end = time.perf_counter()
    ocr_time = end - start

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
    # print(f"FACES:       {len(faces)} detected")
    # if faces:
    #     print("FACE BOXES:  " + ", ".join([f"(x={f['x']}, y={f['y']}, w={f['w']}, h={f['h']})" for f in faces]))
    
    # OCR summary
    print(f"OCR2 TEXT:    {ocr}")
    print(f"OCR TEXT:    {ocr_text}")
    print(f"OCR BOXES:   {len(ocr_boxes)} detected")
    print(f"{'='*40}\n")


    print(f"Time to open: {open_time}")
    print(f"Time to get not gps data: {non_gps_time}")
    print(f"Time to get full data: {gps_time}")
    # print(f"Time to detect faces: {face_time}")
    print(f"Time to extract OCR: {ocr_time}")
    print(f"Time to get caption: {caption_time}")

    
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
    if location_data:
        loc = str(location_data).split(', ')
        metadata['loc_description'] = " ".join(loc[:-3])
        metadata['loc_city'] = loc[-3]
        metadata['loc_state'] = loc[-2]
        metadata['loc_country'] = loc[-1]
    else:
        metadata['loc_description'] = None
        metadata['loc_city'] = None
        metadata['loc_state'] = None
        metadata['loc_country'] = None
 
    metadata['caption'] = output_caption['caption']
    metadata['ocr'] = ocr
    metadata['ocr_text'] = ocr_text
    metadata['ocr_boxes'] = ocr_boxes
    #metadata['faces'] = faces

    metadata["user_id"] = user_id

    save_photo_to_db(conn, metadata)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else 'test_images\\img.jpg'
    curr = init_db()
    if not os.path.exists(target):
        print(f"Input image not found: {target}")
    else:
        get_complete_metadata(target, curr, 1)
        download_from_b2("1/6.jpg", "test_images\\test.jpg")
