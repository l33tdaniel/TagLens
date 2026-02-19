from metadata import *
from pathlib import Path
import time
from upload import *

conn = init_db()

def process_all_images(start_directory: str):
    """
    Recursively finds all images in a directory and its subdirectories,
    then runs a function on each one.
    """
    
    valid_extensions = {
        '.jpg', '.jpeg', '.png', '.heic', '.heif', 
        '.webp', '.bmp', '.tiff'
    }
    
    base_dir = Path(start_directory)
    
    if not base_dir.exists() or not base_dir.is_dir():
        print(f"Error: The directory '{start_directory}' does not exist.")
        return

    print(f"Scanning '{start_directory}' for images...")
    
    image_count = 0
    
    for file_path in base_dir.rglob('*'):
        
        # Check two things: 
        # A) Is it actually a file? (not a folder)
        # B) Does its extension match our list? (.suffix gets the extension, .lower() handles .JPG vs .jpg)
        if file_path.is_file() and file_path.suffix.lower() in valid_extensions:
            
            # Convert the Path object back to a standard string for your other functions
            str_path = str(file_path)
            
            # 6. Call your function!
            print(str_path)
            get_complete_metadata(str_path, conn, 123)
            
            image_count += 1
            
    print(f"\nFinished! Processed {image_count} images.")

# ---------------------------------------------------------
# HOW TO RUN IT
# ---------------------------------------------------------
if __name__ == "__main__":
    # Point this to your main photo folder. 
    # Use standard slashes (/) or raw strings (r"C:\...") for Windows paths.
    my_folder = r"D:\\Photos\\test" 
    
    start = time.time()
    process_all_images(my_folder)
    end = time.time()
    print(end-start)