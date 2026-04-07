# Face detection and tagging module using InsightFace
# Detects faces in images and assigns tags based on facial embeddings
import asyncio
import json
import math
import re
from typing import Any

import cv2
import numpy as np

try:
    from insightface.app import FaceAnalysis
except ImportError:  # Optional dependency - gracefully handle if not installed
    FaceAnalysis = None

# Global cache for the face analyzer instance to avoid reinitializing on every call
_FACE_ANALYZER: Any = None


def _safe_parse_faces(faces_json: str) -> list[dict]:
    """Safely parse JSON string containing face data.
    
    Args:
        faces_json: JSON string representation of faces list
        
    Returns:
        List of face dictionaries, or empty list if parsing fails or input is invalid
    """
    try:
        parsed = json.loads(faces_json)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def public_faces_payload(faces_json: str) -> list[dict]:
    """Convert face data to public API payload format.
    
    Extracts and sanitizes coordinate and tag information, excluding sensitive data like embeddings.
    
    Args:
        faces_json: JSON string of face data
        
    Returns:
        List of sanitized face dictionaries with only x, y, w, h, and tag fields
    """
    faces = _safe_parse_faces(faces_json)
    return [
        {
            "x": int(face.get("x", 0)),
            "y": int(face.get("y", 0)),
            "w": int(face.get("w", 0)),
            "h": int(face.get("h", 0)),
            "tag": str(face.get("tag", "")),
        }
        for face in faces
        if isinstance(face, dict)
    ]


def _init_analyzer() -> Any:
    """Initialize the face analyzer using InsightFace with GPU acceleration.
    
    Uses singleton pattern to avoid creating multiple analyzer instances.
    Falls back to CPU if GPU is unavailable.
    
    Returns:
        FaceAnalysis instance or None if InsightFace is not installed
    """
    global _FACE_ANALYZER
    # Return cached instance if already initialized
    if _FACE_ANALYZER is not None:
        return _FACE_ANALYZER
    # Return None if InsightFace is not installed
    if FaceAnalysis is None:
        return None

    # Initialize analyzer with buffalo_l model (large model, high accuracy)
    analyzer = FaceAnalysis(name="buffalo_l")
    try:
        # Try GPU acceleration (ctx_id=0 is GPU)
        analyzer.prepare(ctx_id=0, det_size=(640, 640))
    except Exception:
        # Fall back to CPU if GPU fails (ctx_id=-1 is CPU)
        analyzer.prepare(ctx_id=-1, det_size=(640, 640))

    _FACE_ANALYZER = analyzer
    return _FACE_ANALYZER


def _to_float_list(values: Any) -> list[float]:
    """Convert values to a list of floats with flexible input handling.
    
    Handles JSON strings, numpy arrays, and lists. Returns empty list on any error.
    
    Args:
        values: Various formats - JSON string, numpy array, list, or scalar
        
    Returns:
        List of floats, or empty list if conversion fails
    """
    if values is None:
        return []
    # Parse JSON strings
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            return []
    # Convert numpy arrays to lists
    if isinstance(values, np.ndarray):
        values = values.tolist()
    # Validate we have a list
    if not isinstance(values, list):
        return []
    # Convert each element to float
    out: list[float] = []
    for value in values:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            return []
    return out


def _serialize_embedding(values: list[float]) -> str:
    """Serialize facial embedding vector to compact JSON string.
    
    Uses minimal separators for compact storage.
    
    Args:
        values: List of embedding values (typically 512 floats)
        
    Returns:
        Compact JSON string representation
    """
    return json.dumps(values, separators=(",", ":"))


def _running_average(
    base: list[float], base_count: int, sample: list[float]
) -> list[float]:
    """Calculate running average of embeddings to improve face recognition accuracy.
    
    Updates the average embedding by incrementally incorporating new samples.
    This helps normalize variations in lighting, angle, and expression.
    
    Args:
        base: Current average embedding
        base_count: Number of samples already in the average
        sample: New embedding sample to incorporate
        
    Returns:
        Updated average embedding
    """
    # If no base or mismatched dimensions, return sample as new baseline
    if not base or len(base) != len(sample):
        return sample
    # Calculate new sample count and update average
    next_count = max(1, int(base_count)) + 1
    return [
        ((float(base[idx]) * (next_count - 1)) + float(sample[idx])) / next_count
        for idx in range(len(sample))
    ]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    """Calculate cosine similarity between two embedding vectors.
    
    Measures how similar two facial embeddings are (0=different, 1=identical).
    Used to determine if two faces belong to the same person.
    
    Args:
        left: First embedding vector
        right: Second embedding vector
        
    Returns:
        Similarity score between 0.0 and 1.0
    """
    # Validate inputs
    if not left or not right or len(left) != len(right):
        return 0.0
    # Calculate dot product
    dot = sum(a * b for a, b in zip(left, right))
    # Calculate magnitudes (L2 norm)
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    # Avoid division by zero
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    # Return cosine similarity
    return dot / (left_norm * right_norm)


def _next_face_tag(known_tags: set[str]) -> str:
    """Generate next unique face tag in sequence.
    
    Finds the highest existing person_N tag and returns the next one.
    
    Args:
        known_tags: Set of existing face tags
        
    Returns:
        Next unique tag (e.g., "person_3")
    """
    max_idx = 0
    # Find the highest numbered person tag
    for tag in known_tags:
        match = re.match(r"^person_(\d+)$", tag)
        if not match:
            continue
        max_idx = max(max_idx, int(match.group(1)))
    return f"person_{max_idx + 1}"


def _detect_faces_with_insightface(image_bytes: bytes) -> list[dict]:
    """Detect faces in image using InsightFace.
    
    Converts image bytes to OpenCV format, detects faces, and extracts
    bounding boxes and facial embeddings.
    
    Args:
        image_bytes: Raw image data in bytes
        
    Returns:
        List of detected faces with coordinates, dimensions, and embeddings
    """
    # Initialize face analyzer
    analyzer = _init_analyzer()
    if analyzer is None:
        return []

    # Decode image from bytes to OpenCV format
    np_buffer = np.frombuffer(image_bytes, dtype=np.uint8)
    bgr_image = cv2.imdecode(np_buffer, cv2.IMREAD_COLOR)
    if bgr_image is None:
        return []

    # Run face detection and embedding extraction
    detected_faces = analyzer.get(bgr_image)
    parsed: list[dict] = []
    for face in detected_faces:
        # Extract bounding box and embedding from detected face
        bbox = getattr(face, "bbox", None)
        embedding = getattr(face, "normed_embedding", None)
        if bbox is None or embedding is None:
            continue
        # Convert bounding box from coordinates to x,y,w,h format
        x1, y1, x2, y2 = [int(v) for v in bbox]
        parsed.append(
            {
                "x": max(0, x1),
                "y": max(0, y1),
                "w": max(0, x2 - x1),
                "h": max(0, y2 - y1),
                "embedding": _to_float_list(embedding),
            }
        )
    return parsed


async def detect_and_tag_faces_for_user(user_id: int, image_bytes: bytes, db: Any) -> list[dict]:
    """Detect and tag faces in an image for a specific user.
    
    Main function that:
    1. Detects new faces in the image
    2. Loads user's known faces from database
    3. Matches new faces to known faces using embeddings
    4. Assigns tags (either existing or new)
    5. Updates embeddings in database
    
    Args:
        user_id: Database user ID
        image_bytes: Raw image data
        db: Database connection/interface object
        
    Returns:
        List of detected faces with assigned tags and embeddings
    """
    # Detect faces in the new image (runs in thread to avoid blocking)
    detected = await asyncio.to_thread(_detect_faces_with_insightface, image_bytes)
    if not detected:
        return []

    # Load user's known faces from database for matching
    known_faces: list[dict] = []
    known_tags: set[str] = set()
    known_from_index = []
    # Try to load from face embeddings index (new method)
    if hasattr(db, "list_face_embeddings_for_user"):
        known_from_index = await db.list_face_embeddings_for_user(user_id)
        for row in known_from_index:
            embedding = _to_float_list(getattr(row, "embedding_json", ""))
            if not row.tag or not embedding:
                continue
            known_faces.append(
                {
                    "tag": row.tag,
                    "embedding": embedding,
                    "samples_count": max(1, int(row.samples_count)),
                }
            )
            known_tags.add(row.tag)

    # Backward-compatible bootstrap: extract embeddings from legacy images.faces_json field
    # This handles images that were tagged before the face embeddings index was created
    if not known_faces and hasattr(db, "list_images_for_user"):
        existing_images = await db.list_images_for_user(user_id)
        by_tag: dict[str, dict] = {}
        # Extract and aggregate embeddings by tag from existing images
        for image in existing_images:
            for face in _safe_parse_faces(image.faces_json):
                if not isinstance(face, dict):
                    continue
                tag = str(face.get("tag", "")).strip()
                embedding = _to_float_list(face.get("embedding"))
                if not tag or not embedding:
                    continue
                # Aggregate multiple samples of same face using running average
                current = by_tag.get(tag)
                if current is None:
                    by_tag[tag] = {
                        "tag": tag,
                        "embedding": embedding,
                        "samples_count": 1,
                    }
                else:
                    averaged = _running_average(
                        current["embedding"], current["samples_count"], embedding
                    )
                    current["embedding"] = averaged
                    current["samples_count"] += 1
                known_tags.add(tag)
        known_faces = list(by_tag.values())
        # Migrate aggregated embeddings to new index for faster future lookups
        if hasattr(db, "upsert_face_embedding_for_user"):
            for known in known_faces:
                await db.upsert_face_embedding_for_user(
                    user_id,
                    known["tag"],
                    _serialize_embedding(known["embedding"]),
                )

    # Match detected faces to known faces and assign tags
    tagged_faces: list[dict] = []
    similarity_threshold = 0.45  # Threshold for considering a match (0-1 scale)

    for face in detected:
        # Extract embedding from detected face
        embedding = _to_float_list(face.get("embedding"))
        if not embedding:
            continue

        # Find best matching known face using cosine similarity
        best_tag = ""
        best_score = -1.0
        for known in known_faces:
            score = _cosine_similarity(embedding, known["embedding"])
            if score > best_score:
                best_score = score
                best_tag = known["tag"]

        # Assign tag: use matched tag if score is high enough, otherwise create new
        if best_tag and best_score >= similarity_threshold:
            assigned_tag = best_tag
        else:
            # Create new person tag for unrecognized face
            assigned_tag = _next_face_tag(known_tags)
            known_tags.add(assigned_tag)

        # Update or create known face entry with new embedding
        known_face = next((item for item in known_faces if item["tag"] == assigned_tag), None)
        if known_face is None:
            # New face: initialize with this embedding
            updated_embedding = embedding
            updated_samples = 1
            known_face = {
                "tag": assigned_tag,
                "embedding": updated_embedding,
                "samples_count": updated_samples,
            }
            known_faces.append(known_face)
        else:
            # Known face: update embedding average and sample count
            updated_embedding = _running_average(
                known_face["embedding"], known_face["samples_count"], embedding
            )
            updated_samples = known_face["samples_count"] + 1
            known_face["embedding"] = updated_embedding
            known_face["samples_count"] = updated_samples
        # Persist updated embedding to database
        if hasattr(db, "upsert_face_embedding_for_user"):
            await db.upsert_face_embedding_for_user(
                user_id, assigned_tag, _serialize_embedding(updated_embedding)
            )

        # Build output payload for this face
        face_payload = {
            "x": int(face.get("x", 0)),
            "y": int(face.get("y", 0)),
            "w": int(face.get("w", 0)),
            "h": int(face.get("h", 0)),
            "tag": assigned_tag,
            "embedding": embedding,
        }
        tagged_faces.append(face_payload)

    return tagged_faces
