import os
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import pillow_heif
from geopy.geocoders import Nominatim # <--- New library

# Setup
pillow_heif.register_heif_opener()
# Note: "user_agent" can be any name; it just identifies your script to the server
geolocator = Nominatim(user_agent="my_photo_metadata_tool")

def get_complete_metadata(path):
    img = Image.open(path)
    
    # 1. Basic Stats
    file_size_mb = os.path.getsize(path) / (1024 * 1024)
    w, h = img.size
    mp = (w * h) / 1_000_000

    # 2. Get Metadata Folders
    exif = img.getexif()
    exif_ifd = exif.get_ifd(34665)  # Technical details
    gps_info = exif.get_ifd(34853)  # GPS details

    # 3. Device & Camera Info
    make = exif.get(271, "Unknown")
    model = exif.get(272, "Unknown")
    date = exif.get(36867) or exif.get(306) or "Unknown Date"
    
    # Technical specs
    iso = exif_ifd.get(34855, "N/A")
    f_stop = exif_ifd.get(33437, "N/A")
    focal = exif_ifd.get(37386, "N/A")
    exposure = exif_ifd.get(33434, "N/A")
    shutter = f"1/{int(1/exposure)}" if (isinstance(exposure, (int, float)) and exposure < 1) else f"{exposure}"
    
    # 4. Location Name (The "Google Photos" Style)
    location_name = "No GPS Data"
    lat_lon_str = "N/A"

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

    # --- FINAL OUTPUT ---
    print(f"\n{'='*40}")
    print(f"FILE:        {os.path.basename(path)}")
    print(f"SIZE:        {file_size_mb:.2f} MB")
    print(f"QUALITY:     {w}x{h} ({mp:.2f}MP)")
    print(f"DEVICE:      {make} {model}")
    print(f"DATE:        {date}")
    print(f"SETTINGS:    ƒ/{f_stop} | {shutter}s | {focal}mm | ISO {iso}")
    print(f"COORDS:      {lat_lon_str}")
    print(f"LOCATION:    {location_data}")
    print(f"{'='*40}\n")

# Run it
get_complete_metadata('test_images/photo.HEIC')
