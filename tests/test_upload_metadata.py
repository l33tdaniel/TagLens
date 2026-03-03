import io

from PIL import Image

from scripts.upload_metadata import extract_upload_metadata


def test_extract_upload_metadata_handles_invalid_bytes() -> None:
    metadata = extract_upload_metadata(b"not-an-image")
    assert metadata.ocr_text == ""
    assert metadata.taken_at is None


def test_extract_upload_metadata_reads_exif_taken_at() -> None:
    image = Image.new("RGB", (20, 20), (255, 255, 255))
    exif = Image.Exif()
    exif[36867] = "2025:01:10 12:34:56"
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    payload = buffer.getvalue()

    metadata = extract_upload_metadata(payload)
    assert metadata.taken_at == "2025-01-10T12:34:56"
