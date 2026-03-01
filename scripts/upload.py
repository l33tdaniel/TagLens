from b2sdk.v2 import InMemoryAccountInfo, B2Api
from dotenv import load_dotenv
import os

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
load_dotenv()
KEY_ID = os.getenv("KEY_ID")
APP_KEY = os.getenv("APP_KEY")
BUCKET_NAME = os.getenv("BUCKET_NAME")


LOCAL_FILE_PATH = "test_images\\IMG_8700.heif"  # File on your computer
B2_FILE_NAME = "test/image.heif"  # What you want to call it in the cloud


# 1. Initialize the API
info = InMemoryAccountInfo()
b2_api = B2Api(info)

# 2. Authorize
b2_api.authorize_account("production", KEY_ID, APP_KEY)
# 3. Get the Bucket
bucket = b2_api.get_bucket_by_name(BUCKET_NAME)


def upload_to_b2(local_path, b2_name):
    try:
        # 4. Upload the File
        file_info = bucket.upload_local_file(local_file=local_path, file_name=b2_name)

        # Return the file ID silently upon success
        return file_info.id_

    except Exception as e:
        # Only print if an error occurs, showing the path and the reason
        print(f"Failed to upload: {local_path} | Error: {e}")

        # Return None so your main script knows this specific file failed
        return None


# TEMP
def download_from_b2(b2_file_path, local_save_path):
    print(f"Connecting to Backblaze to find: {b2_file_path}...")

    try:
        # 1. Ensure the folder exists locally
        os.makedirs(os.path.dirname(local_save_path), exist_ok=True)

        print(f"Downloading to {local_save_path}...")

        # 2. Get the download object wrapper
        downloaded_file = bucket.download_file_by_name(b2_file_path)

        # 3. FIX: Open the local file in 'write binary' mode ('wb')
        # This creates the file object that has the .seekable() attribute
        with open(local_save_path, "wb") as f:
            downloaded_file.save(f)

        print("Download complete!")
        return True

    except Exception as e:
        print(f"Error: Could not download file. {e}")
        return False


# # Run it
if __name__ == "__main__":
    # file_id = upload_to_b2(LOCAL_FILE_PATH, B2_FILE_NAME)
    success = download_from_b2("1/5.heif", "test_images\\test.heif")
