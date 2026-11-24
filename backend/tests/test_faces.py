"""Face detection tests.

The detector runs on a real photograph; the fixture lives in ``.artifacts`` and is
generated/downloaded, never committed (it is a stranger's face, not test data we
own). Tests skip cleanly when the model or the photo is missing, so a fresh clone
still runs the suite.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from app.faces import DEFAULT_YUNET, YuNetDetector, make_detector

ROOT = Path(__file__).resolve().parents[2]
PHOTO = ROOT / ".artifacts" / "face-test.jpg"

pytestmark = pytest.mark.skipif(
    not DEFAULT_YUNET.is_file() or not PHOTO.is_file(),
    reason="face model or test photo missing (see README: YuNet + .artifacts/face-test.jpg)",
)


def _data_url(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()


def test_detects_a_real_face() -> None:
    detection = YuNetDetector().detect(_data_url(PHOTO))
    assert detection is not None, "no face found in the test photo"
    assert detection.confidence >= 0.6
    assert detection.thumbnail is not None
    assert detection.thumbnail.startswith("data:image/jpeg;base64,")
    # The crop must be real image data, not an empty string.
    decoded = base64.b64decode(detection.thumbnail.split(",", 1)[1])
    assert len(decoded) > 2000
    assert detection.box is not None


def test_blank_image_has_no_face() -> None:
    import cv2
    import numpy as np

    blank = np.full((480, 640, 3), 200, dtype=np.uint8)
    ok, buffer = cv2.imencode(".jpg", blank)
    assert ok
    data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode()
    assert YuNetDetector().detect(data_url) is None


def test_detector_selection_prefers_what_works() -> None:
    detector = make_detector("auto")
    assert detector.name in {"yunet", "insightface", "none"}
    if DEFAULT_YUNET.is_file():
        assert detector.name != "none"


def test_broken_frame_does_not_raise() -> None:
    assert YuNetDetector().detect("data:image/jpeg;base64,not-an-image") is None
