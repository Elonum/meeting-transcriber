"""The face crop must look good in a 4:3 tile: whole face, no second crop.

The tile shows the crop with ``object-fit: cover``. If the crop is not 4:3, CSS
cuts it again and the mouth disappears — the reported bug.
"""

from __future__ import annotations

import base64

import numpy as np
import pytest
from app.faces import CROP_ASPECT, face_crop

cv2 = pytest.importorskip("cv2")


def image(width: int = 640, height: int = 480) -> np.ndarray:
    picture = np.full((height, width, 3), 40, dtype=np.uint8)
    # A head-like ellipse so the crop has something to show.
    cv2.ellipse(picture, (width // 2, height // 2), (60, 80), 0, 0, 360, (200, 180, 160), -1)
    return picture


def decode(data_url: str) -> np.ndarray:
    _, payload = data_url.split(",", 1)
    raw = base64.b64decode(payload)
    picture = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert picture is not None
    return picture


def test_crop_is_four_by_three() -> None:
    picture = image()
    box = (280, 160, 80, 100)

    crop = decode(face_crop(picture, box))

    height, width = crop.shape[:2]
    assert width / height == pytest.approx(CROP_ASPECT, rel=0.02)


def test_crop_keeps_the_mouth() -> None:
    """The face box ends at the chin; the crop must include what is below it."""
    picture = image()
    box = (280, 160, 80, 100)
    # Paint a magenta band 30 px below the box: that is the mouth/chin area.
    picture[260:280, 300:340] = (255, 0, 255)

    crop = decode(face_crop(picture, box))

    magenta = np.count_nonzero((crop[:, :, 0] > 200) & (crop[:, :, 2] > 200))
    assert magenta > 0, "the crop cut the mouth off"


def test_crop_stays_inside_the_frame_at_the_edge() -> None:
    picture = image()
    box = (0, 0, 60, 70)  # a face hugging the top-left corner

    crop = decode(face_crop(picture, box))

    height, width = crop.shape[:2]
    assert width / height == pytest.approx(CROP_ASPECT, rel=0.02)
    assert width <= picture.shape[1] and height <= picture.shape[0]


def test_crop_respects_max_width() -> None:
    picture = image(1920, 1080)
    box = (900, 400, 200, 240)

    crop = decode(face_crop(picture, box, max_width=320))

    assert crop.shape[1] == 320
    assert crop.shape[1] / crop.shape[0] == pytest.approx(CROP_ASPECT, rel=0.02)


def test_small_crop_is_not_upscaled() -> None:
    picture = image(320, 240)
    box = (120, 80, 60, 70)

    crop = decode(face_crop(picture, box, max_width=320))

    assert crop.shape[1] <= 320


def test_a_tiny_frame_does_not_break_the_aspect() -> None:
    picture = image(120, 90)
    box = (40, 30, 30, 40)

    crop = decode(face_crop(picture, box))

    height, width = crop.shape[:2]
    assert width / height == pytest.approx(CROP_ASPECT, rel=0.05)


@pytest.mark.parametrize("box", [(0, 0, 0, 10), (10, 10, 10, 0), (-50, -50, 10, 10)])
def test_degenerate_boxes_return_nothing(box) -> None:
    """A zero-size or fully off-frame box is not a face, but a box half outside
    the picture is still cropped (the person simply sits at the edge)."""
    assert face_crop(image(), box) is None


def test_a_box_partly_outside_is_still_cropped() -> None:
    assert face_crop(image(), (-10, -10, 60, 70)) is not None


def test_result_is_a_jpeg_data_url() -> None:
    data_url = face_crop(image(), (280, 160, 80, 100))

    assert data_url is not None
    assert data_url.startswith("data:image/jpeg;base64,")
    payload = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    assert payload.startswith(b"\xff\xd8"), "JPEG SOI marker"
