import asyncio
import json
import math
import re
from typing import Any

import cv2
import numpy as np

try:
    from insightface.app import FaceAnalysis
except ImportError:  #  optional dependency
    FaceAnalysis = None


_FACE_ANALYZER: Any = None


def _safe_parse_faces(faces_json: str) -> list[dict]:
    try:
        parsed = json.loads(faces_json)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def public_faces_payload(faces_json: str) -> list[dict]:
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
    global _FACE_ANALYZER
    if _FACE_ANALYZER is not None:
        return _FACE_ANALYZER
    if FaceAnalysis is None:
        return None

    analyzer = FaceAnalysis(name="buffalo_l")
    try:
        analyzer.prepare(ctx_id=0, det_size=(640, 640))
    except Exception:
        analyzer.prepare(ctx_id=-1, det_size=(640, 640))

    _FACE_ANALYZER = analyzer
    return _FACE_ANALYZER


def _to_float_list(values: Any) -> list[float]:
    if values is None:
        return []
    if isinstance(values, np.ndarray):
        values = values.tolist()
    if not isinstance(values, list):
        return []
    out: list[float] = []
    for value in values:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            return []
    return out


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _next_face_tag(known_tags: set[str]) -> str:
    max_idx = 0
    for tag in known_tags:
        match = re.match(r"^person_(\d+)$", tag)
        if not match:
            continue
        max_idx = max(max_idx, int(match.group(1)))
    return f"person_{max_idx + 1}"


def _detect_faces_with_insightface(image_bytes: bytes) -> list[dict]:
    analyzer = _init_analyzer()
    if analyzer is None:
        return []

    np_buffer = np.frombuffer(image_bytes, dtype=np.uint8)
    bgr_image = cv2.imdecode(np_buffer, cv2.IMREAD_COLOR)
    if bgr_image is None:
        return []

    detected_faces = analyzer.get(bgr_image)
    parsed: list[dict] = []
    for face in detected_faces:
        bbox = getattr(face, "bbox", None)
        embedding = getattr(face, "normed_embedding", None)
        if bbox is None or embedding is None:
            continue
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
    detected = await asyncio.to_thread(_detect_faces_with_insightface, image_bytes)
    if not detected:
        return []

    existing_images = await db.list_images_for_user(user_id)
    known_faces: list[dict] = []
    known_tags: set[str] = set()
    for image in existing_images:
        for face in _safe_parse_faces(image.faces_json):
            if not isinstance(face, dict):
                continue
            tag = str(face.get("tag", "")).strip()
            embedding = _to_float_list(face.get("embedding"))
            if not tag or not embedding:
                continue
            known_faces.append({"tag": tag, "embedding": embedding})
            known_tags.add(tag)

    tagged_faces: list[dict] = []
    similarity_threshold = 0.45

    for face in detected:
        embedding = _to_float_list(face.get("embedding"))
        if not embedding:
            continue

        best_tag = ""
        best_score = -1.0
        for known in known_faces:
            score = _cosine_similarity(embedding, known["embedding"])
            if score > best_score:
                best_score = score
                best_tag = known["tag"]

        if best_tag and best_score >= similarity_threshold:
            assigned_tag = best_tag
        else:
            assigned_tag = _next_face_tag(known_tags)
            known_tags.add(assigned_tag)

        face_payload = {
            "x": int(face.get("x", 0)),
            "y": int(face.get("y", 0)),
            "w": int(face.get("w", 0)),
            "h": int(face.get("h", 0)),
            "tag": assigned_tag,
            "embedding": embedding,
        }
        tagged_faces.append(face_payload)
        known_faces.append({"tag": assigned_tag, "embedding": embedding})

    return tagged_faces
