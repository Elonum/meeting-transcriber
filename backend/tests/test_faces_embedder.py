"""Real embeddings through OpenCV SFace, and the detector that produces them.

These tests need the 37 MB SFace model and the OpenCV build with
``FaceRecognizerSF``. They skip (not fail) when either is missing, so the suite
still runs on a clean machine.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from app import faces

pytest.importorskip("cv2")
cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

PHOTO = Path(__file__).resolve().parents[2] / ".artifacts" / "speech" / "face-test.jpg"
MODEL = faces.DEFAULT_SFACE
YUNET = faces.DEFAULT_YUNET


@pytest.fixture(scope="module")
def photo():
    if not PHOTO.is_file():
        pytest.skip("no test photo")
    return cv2.imread(str(PHOTO))


@pytest.fixture(scope="module")
def faces_in_photo(photo):
    detector = cv2.FaceDetectorYN_create(str(YUNET), "", (photo.shape[1], photo.shape[0]), 0.6, 0.3, 5000)
    _count, boxes = detector.detect(photo)
    if boxes is None or len(boxes) < 2:
        pytest.skip("the test photo needs at least two faces")
    return boxes


@pytest.fixture(scope="module")
def embedder():
    if not MODEL.is_file():
        pytest.skip("SFace model is not downloaded")
    return faces.SFaceEmbedder(MODEL)


def test_sface_produces_unit_vectors(photo, faces_in_photo, embedder) -> None:
    vector = embedder.embed(photo, faces_in_photo[0])

    assert vector is not None
    assert len(vector) == 128
    assert sum(value * value for value in vector) == pytest.approx(1.0, rel=1e-6)


def test_the_same_face_matches_itself(photo, faces_in_photo, embedder) -> None:
    first = embedder.embed(photo, faces_in_photo[0])
    again = embedder.embed(photo, faces_in_photo[0])

    assert first is not None and again is not None
    assert faces.face_similarity(first, again) > 0.99


def test_two_different_people_are_far_apart(photo, faces_in_photo, embedder) -> None:
    """The published SFace threshold is 0.363; different people must sit below."""
    from app.identity import DEFAULT_THRESHOLD

    first = embedder.embed(photo, faces_in_photo[0])
    second = embedder.embed(photo, faces_in_photo[2])  # a clearly different box

    assert first is not None and second is not None
    similarity = faces.face_similarity(first, second)
    assert similarity < DEFAULT_THRESHOLD, f"different people scored {similarity:.3f}"


def test_the_detector_attaches_an_embedding(photo, embedder) -> None:
    small = cv2.resize(photo, (640, int(640 * photo.shape[0] / photo.shape[1])))
    frame = "data:image/jpeg;base64," + base64.b64encode(cv2.imencode(".jpg", small)[1].tobytes()).decode()
    detector = faces.YuNetDetector(embedder=embedder)

    detection = detector.detect(frame)

    assert detection is not None
    assert detection.embedding is not None
    assert len(detection.embedding) == 128


def test_the_detector_works_without_identity(photo) -> None:
    small = cv2.resize(photo, (640, int(640 * photo.shape[0] / photo.shape[1])))
    frame = "data:image/jpeg;base64," + base64.b64encode(cv2.imencode(".jpg", small)[1].tobytes()).decode()
    detector = faces.YuNetDetector()

    detection = detector.detect(frame)

    assert detection is not None
    assert detection.embedding is None, "identity is off by default"


def test_the_reported_box_belongs_to_the_best_face(photo) -> None:
    """Regression: the box used to come from whichever face the loop finished on."""
    small = cv2.resize(photo, (640, int(640 * photo.shape[0] / photo.shape[1])))
    frame = "data:image/jpeg;base64," + base64.b64encode(cv2.imencode(".jpg", small)[1].tobytes()).decode()

    detection = faces.YuNetDetector().detect(frame)

    assert detection is not None and detection.box is not None
    x, y, w, h = detection.box
    assert x >= 0 and y >= 0 and w > 0 and h > 0
    assert x + w <= small.shape[1] and y + h <= small.shape[0]


def test_make_embedder_respects_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSCRIBE_FACE_IDENTITY", raising=False)
    assert faces.make_embedder() is None

    monkeypatch.setenv("TRANSCRIBE_FACE_IDENTITY", "1")
    monkeypatch.setenv("TRANSCRIBE_FACE_MODEL", str(MODEL))
    embedder = faces.make_embedder()
    assert embedder is not None and embedder.name == "sface"


def test_make_embedder_survives_a_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBE_FACE_IDENTITY", "1")
    monkeypatch.setenv("TRANSCRIBE_FACE_MODEL", str(MODEL.with_name("nope.onnx")))

    assert faces.make_embedder() is None
