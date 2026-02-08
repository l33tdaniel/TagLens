import os
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
if torch.cuda.is_available():
    device = "cuda"
elif torch.backends.mps.is_available():
    device = "mps"
else:
    device = "cpu"

print(f"Using device: {device}")

model = AutoModelForCausalLM.from_pretrained(
    "vikhyatk/moondream2",
    trust_remote_code=True,
    dtype=torch.bfloat16,
    device_map=device,
)

def get_complete_metadata(path):
    start = time.perf_counter()
    img = Image.open(path)
    end = time.perf_counter()
    open_time = end - start
    
    start = time.perf_counter()
    # 1. Basic Stats
    file_size_mb = os.path.getsize(path) / (1024 * 1024)
    w, h = img.size
    mp = (w * h) / 1_000_000


    # 2. Get Metadata Folders
    exif = img.getexif()
    exif_ifd = exif.get_ifd(34665)  # Technical details
    gps_info = exif.get_ifd(34853)  # GPS details

    # 3. Device & Camera Info
    phone_make = exif.get(271, "Unknown")
    phone_model = exif.get(272, "Unknown")
    date = exif.get(36867) or exif.get(306) or "Unknown Date"
    
    # Technical specs
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
            # This reaches out to OpenStreetMap to get the name
            location_data = geolocator.reverse((lat, lon), language='en')
        except Exception as e:
            location_data = None
    end = time.perf_counter()
    metadata_time = end-start

    start = time.perf_counter()
    output_caption = model.caption(
        img, 
        length="short", 
        # settings=settings
    )
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
    print(f"CAPTION:     {output_caption}")
    print(f"{'='*40}\n")


    print(f"Time to open: {open_time}")
    print(f"Time to get not gps data: {non_gps_time}")
    print(f"Time to get full data: {metadata_time}")
    print(f"Time to get caption: {caption_time}")


# Run it
get_complete_metadata('test_images/photo.HEIC')
