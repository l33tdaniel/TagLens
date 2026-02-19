from metadata import *
from pathlib import Path
import time
from upload import *


def process_all_images(start_directory: str, conn, user_id):
    """
    Recursively finds all images in a directory and its subdirectories,
    then runs a function on each one.
    """
    
    valid_extensions = {
        '.jpg', '.jpeg', '.png', '.heic', '.heif', 
        '.webp', '.bmp', '.tiff'
   }

    valid_video_extensions = {
        '.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v'
    }
     
    base_dir = Path(start_directory)
    
    if not base_dir.exists() or not base_dir.is_dir():
        print(f"Error: The directory '{start_directory}' does not exist.")
        return

    print(f"Scanning '{start_directory}' for images...")
    
    image_count = 0
    video_count = 0
    
    for file_path in base_dir.rglob('*'):
        
        # Check two things: 
        # A) Is it actually a file? (not a folder)
        # B) Does its extension match our list? (.suffix gets the extension, .lower() handles .JPG vs .jpg)
        if file_path.is_file(): 

            str_path = str(file_path)
            ext = file_path.suffix.lower()

            if ext in valid_extensions:
                print(f"Found Image: {str_path}")
                get_complete_metadata(str_path, conn, user_id)
                image_count += 1
                
            elif ext in valid_video_extensions:
                print(f"Found Video: {str_path}")
                handle_video(str_path, conn, user_id)
                video_count += 1
            

            
    print(f"\nFinished! Processed {image_count} images.")
    print(f"\nFinished! Processed {video_count} videos.")


# ---------------------------------------------------------
# HOW TO RUN IT
# ---------------------------------------------------------
if __name__ == "__main__":
    # Point this to your main photo folder. 
    # Use standard slashes (/) or raw strings (r"C:\...") for Windows paths.
    f1 = r"D:\Photos\Takeout" 
    f2 = r"D:\Photos\takeout-20260212T234250Z-3-001\Takeout"

    conn = init_db()

    
    start = time.time()
    process_all_images(f1, conn, 123)
    process_all_images(f2, conn, 123)
    end = time.time()
    

    print(end-start)