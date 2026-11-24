"""Face detection behind a small interface.

Status of this module
---------------------
The capture side already gives us, per participant, a JPEG frame at native
resolution (``window.__grabFace``). What is missing on this machine is a
detector: InsightFace needs ``onnxruntime`` and a compiled extension, and neither
has wheels for Python 3.14 yet. So the pipeline runs with ``NullDetector``,
reports ``status="frame"`` (a frame arrived, no detection), and the UI shows the
real participant video instead of pretending a face was matched.

``InsightFaceDetector`` is written and ready; it activates as soon as the ML venv
(Python 3.12) exists. The interface is deliberately tiny: the pipeline never
learns which library is behind it.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

#: A face smaller than this (in pixels) is not worth reporting.
MIN_FACE_PX = 32


@dataclass(slots=True)
class Detection:
    """One detected face inside a participant's frame."""

    confidence: float
    #: Cropped, downscaled JPEG data URL for the tile avatar.
    thumbnail: str | None = None
    box: tuple[int, int, int, int] | None = None


class FaceDetector(Protocol):
    name: str

    def detect(self, frame_data_url: str) -> Detection | None: ...


class NullDetector:
    """No detector installed: frames pass through unchanged."""

    name = "none"

    def detect(self, frame_data_url: str) -> Detection | None:  # noqa: ARG002
        return None


class InsightFaceDetector:
    """InsightFace (SCRFD detect + ArcFace embed), loaded lazily.

    Requires the ML venv: ``pip install insightface onnxruntime-gpu``. The model
    is downloaded to ``~/.insightface`` on first use.
    """

    name = "insightface"

    def __init__(self, *, max_width: int = 320, min_confidence: float = 0.5) -> None:
        self.max_width = max_width
        self.min_confidence = min_confidence
        self._app = None

    def _ensure(self):
        if self._app is not None:
            return self._app
        import insightface
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(
            name="buffalo_l",
            allowed_modules=["detection"],
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        app.prepare(ctx_id=0, det_size=(640, 480))
        self._app = app
        log.info("insightface ready (detection only)")
        return app

    def detect(self, frame_data_url: str) -> Detection | None:
        import numpy as np
        from PIL import Image
        import io

        payload = frame_data_url.split(",", 1)[-1]
        raw = base64.b64decode(payload)
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        array = np.asarray(image)[:, :, ::-1]  # RGB -> BGR for insightface

        faces = self._ensure().get(array)
        best = None
        for face in faces:
            score = float(getattr(face, "det_score", 0.0))
            x1, y1, x2, y2 = (int(v) for v in face.bbox)
            if score < self.min_confidence or min(x2 - x1, y2 - y1) < MIN_FACE_PX:
                continue
            if best is None or score > best[0]:
                best = (score, (x1, y1, x2, y2), face.kps)
        if best is None:
            return None

        score, box, _kps = best
        x1, y1, x2, y2 = box
        pad = int(0.25 * max(x2 - x1, y2 - y1))
        crop = image.crop(
            (max(0, x1 - pad), max(0, y1 - pad), min(image.width, x2 + pad), min(image.height, y2 + pad))
        )
        width = min(self.max_width, crop.width)
        crop = crop.resize((width, int(crop.height * width / max(1, crop.width))))
        buffer = io.BytesIO()
        crop.save(buffer, format="JPEG", quality=80)
        thumbnail = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
        return Detection(confidence=round(score, 3), thumbnail=thumbnail, box=box)


def make_detector(kind: str = "auto") -> FaceDetector:
    """Pick the best detector that actually imports on this interpreter."""
    if kind in ("auto", "insightface"):
        try:
            import insightface  # noqa: F401
            import onnxruntime  # noqa: F401

            return InsightFaceDetector()
        except Exception:  # noqa: BLE001 - expected on Python 3.14, see module docs
            if kind == "insightface":
                log.warning("insightface requested but unavailable; using no detection")
    return NullDetector()
