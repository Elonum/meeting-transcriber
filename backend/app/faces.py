"""Face detection behind a small interface.

Current state
-------------
``YuNetDetector`` is real and active: OpenCV's built-in ``FaceDetectorYN`` runs a
232 KB ONNX model (OpenCV Zoo, 2023mar) on the participant frame the browser
already hands us. It has wheels for Python 3.14, so no second interpreter is
needed.

``InsightFaceDetector`` stays here for the moment identity matters (ArcFace
embeddings, matching a name from a roster). It needs Python 3.12 — no cp314 wheels
for insightface — and is not required for "which face belongs to which speaker",
because that mapping already comes from the audio/video track.

Scope note: this is *detection*, not identification. We report that a face was
found in a participant's stream and show the crop; we do not match people against
a biometric database (see the legal section of the README).
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_YUNET = ROOT / ".models" / "face_detection_yunet_2023mar.onnx"

#: A face smaller than this (in pixels of the original frame) is not worth reporting.
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


def _decode(frame_data_url: str) -> bytes:
    try:
        return base64.b64decode(frame_data_url.split(",", 1)[-1], validate=True)
    except Exception:  # noqa: BLE001 - a malformed frame is not an error worth raising
        return b""


class YuNetDetector:
    """OpenCV FaceDetectorYN: fast, tiny, no extra model zoo to manage."""

    name = "yunet"

    def __init__(
        self,
        model_path: Path | str | None = None,
        *,
        max_width: int = 320,
        score_threshold: float = 0.6,
    ) -> None:
        self.model_path = Path(model_path) if model_path else DEFAULT_YUNET
        self.max_width = max_width
        self.score_threshold = score_threshold
        self._detector: Any = None

    def available(self) -> bool:
        return self.model_path.is_file()

    def _ensure(self) -> Any:
        if self._detector is not None:
            return self._detector
        import cv2

        self._detector = cv2.FaceDetectorYN_create(
            str(self.model_path), "", (640, 480), self.score_threshold, 0.3, 5000
        )
        log.info("yunet ready (%s)", self.model_path.name)
        return self._detector

    def detect(self, frame_data_url: str) -> Detection | None:
        import cv2
        import numpy as np

        raw = _decode(frame_data_url)
        if not raw:
            return None
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return None

        height, width = image.shape[:2]
        detector = self._ensure()
        detector.setInputSize((width, height))
        _count, faces = detector.detect(image)
        if faces is None or len(faces) == 0:
            return None

        best = None
        for face in faces:
            score = float(face[-1])
            x, y, w, h = (int(value) for value in face[:4])
            if score < self.score_threshold or min(w, h) < MIN_FACE_PX:
                continue
            if best is None or score > best[0]:
                best = (score, (x, y, w, h))
        if best is None:
            return None

        score, (x, y, w, h) = best
        pad_x, pad_y = int(0.3 * w), int(0.35 * h)
        left = max(0, x - pad_x)
        top = max(0, y - pad_y)
        right = min(width, x + w + pad_x)
        bottom = min(height, y + h + pad_y)
        crop = image[top:bottom, left:right]
        if crop.size == 0:
            return None

        scale = min(1.0, self.max_width / max(1, crop.shape[1]))
        if scale < 1.0:
            crop = cv2.resize(crop, (int(crop.shape[1] * scale), int(crop.shape[0] * scale)))
        ok, buffer = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            return None
        thumbnail = "data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode()
        return Detection(
            confidence=round(score, 3),
            thumbnail=thumbnail,
            box=(x, y, w, h),
        )


class InsightFaceDetector:
    """InsightFace (SCRFD + ArcFace embeddings), for when identity is needed.

    Requires the Python 3.12 venv: ``pip install insightface onnxruntime-gpu``.
    """

    name = "insightface"

    def __init__(self, *, max_width: int = 320, min_confidence: float = 0.5) -> None:
        self.max_width = max_width
        self.min_confidence = min_confidence
        self._app: Any = None

    def _ensure(self) -> Any:
        if self._app is not None:
            return self._app
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(
            name="buffalo_l",
            allowed_modules=["detection", "recognition"],
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        app.prepare(ctx_id=0, det_size=(640, 480))
        self._app = app
        log.info("insightface ready")
        return app

    def detect(self, frame_data_url: str) -> Detection | None:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(_decode(frame_data_url), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return None
        faces = self._ensure().get(image)
        if not faces:
            return None
        best = max(faces, key=lambda face: float(getattr(face, "det_score", 0.0)))
        score = float(getattr(best, "det_score", 0.0))
        if score < self.min_confidence:
            return None
        x1, y1, x2, y2 = (int(value) for value in best.bbox)
        pad = int(0.25 * max(x2 - x1, y2 - y1))
        crop = image[max(0, y1 - pad) : y2 + pad, max(0, x1 - pad) : x2 + pad]
        if crop.size == 0:
            return None
        scale = min(1.0, self.max_width / max(1, crop.shape[1]))
        if scale < 1.0:
            crop = cv2.resize(crop, (int(crop.shape[1] * scale), int(crop.shape[0] * scale)))
        ok, buffer = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            return None
        return Detection(
            confidence=round(score, 3),
            thumbnail="data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode(),
            box=(x1, y1, x2 - x1, y2 - y1),
        )


def make_detector(kind: str = "auto") -> FaceDetector:
    """Pick the best detector that actually works on this interpreter."""
    if kind in ("auto", "insightface"):
        try:
            import insightface  # noqa: F401
            import onnxruntime  # noqa: F401

            return InsightFaceDetector()
        except Exception:  # noqa: BLE001 - expected on Python 3.14
            if kind == "insightface":
                log.warning("insightface requested but unavailable")

    if kind in ("auto", "yunet"):
        candidate = YuNetDetector()
        if candidate.available():
            try:
                import cv2  # noqa: F401

                return candidate
            except Exception:  # noqa: BLE001
                log.warning("opencv missing, face detection disabled")

    return NullDetector()
