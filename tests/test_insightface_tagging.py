import json
from types import SimpleNamespace

import pytest

from scripts import insightface_tagging as tagging


class _FakeDb:
    def __init__(self) -> None:
        self.embedding_rows = []
        self.images = []
        self.upserts = []

    async def list_face_embeddings_for_user(self, user_id: int):
        return self.embedding_rows

    async def list_images_for_user(self, user_id: int):
        return self.images

    async def upsert_face_embedding_for_user(
        self, user_id: int, tag: str, embedding_json: str
    ) -> None:
        self.upserts.append((user_id, tag, embedding_json))


@pytest.mark.asyncio
async def test_detect_and_tag_faces_matches_known_and_creates_new(monkeypatch) -> None:
    db = _FakeDb()
    db.embedding_rows = [
        SimpleNamespace(
            user_id=1,
            tag="person_1",
            embedding_json=json.dumps([1.0, 0.0]),
            samples_count=1,
            updated_at="2025-01-01T00:00:00",
        )
    ]

    def _fake_detect(_: bytes) -> list[dict]:
        return [
            {"x": 1, "y": 2, "w": 3, "h": 4, "embedding": [0.99, 0.01]},
            {"x": 5, "y": 6, "w": 7, "h": 8, "embedding": [0.0, 1.0]},
        ]

    monkeypatch.setattr(tagging, "_detect_faces_with_insightface", _fake_detect)
    faces = await tagging.detect_and_tag_faces_for_user(1, b"image", db)
    assert [face["tag"] for face in faces] == ["person_1", "person_2"]
    assert len(db.upserts) >= 2


@pytest.mark.asyncio
async def test_detect_and_tag_faces_bootstraps_from_legacy_faces_json(monkeypatch) -> None:
    db = _FakeDb()
    db.embedding_rows = []
    db.images = [
        SimpleNamespace(
            faces_json=json.dumps(
                [{"tag": "person_7", "embedding": [0.9, 0.1], "x": 0, "y": 0, "w": 1, "h": 1}]
            )
        )
    ]

    def _fake_detect(_: bytes) -> list[dict]:
        return [{"x": 1, "y": 1, "w": 2, "h": 2, "embedding": [0.91, 0.09]}]

    monkeypatch.setattr(tagging, "_detect_faces_with_insightface", _fake_detect)
    faces = await tagging.detect_and_tag_faces_for_user(9, b"image", db)
    assert len(faces) == 1
    assert faces[0]["tag"] == "person_7"
    assert any(tag == "person_7" for _, tag, _ in db.upserts)


@pytest.mark.asyncio
async def test_detect_and_tag_faces_returns_empty_when_detection_unavailable(monkeypatch) -> None:
    db = _FakeDb()

    def _fake_detect(_: bytes) -> list[dict]:
        return []

    monkeypatch.setattr(tagging, "_detect_faces_with_insightface", _fake_detect)
    faces = await tagging.detect_and_tag_faces_for_user(1, b"image", db)
    assert faces == []
    assert db.upserts == []
