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

Identification is optional and off by default (``TRANSCRIBE_FACE_IDENTITY=1``).
When it is on, ``SFaceEmbedder`` turns the same crop into a 128-number vector, so
the meeting can tell "the same person came back" from "somebody new joined". The
vector lives in memory for the duration of the meeting only — see
``app.identity`` and the privacy section of the README.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

log = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only, numpy stays a lazy import
    import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_YUNET = ROOT / ".models" / "face_detection_yunet_2023mar.onnx"
#: OpenCV Zoo SFace (2021dec): 37 MB, 128-dimensional embeddings, no extra wheels.
DEFAULT_SFACE = ROOT / ".models" / "face_recognition_sface_2021dec.onnx"

#: A face smaller than this (in pixels of the original frame) is not worth reporting.
MIN_FACE_PX = 32


@dataclass(slots=True)
class Detection:
    """One detected face inside a participant's frame."""

    confidence: float
    #: Cropped, downscaled JPEG data URL for the tile avatar.
    thumbnail: str | None = None
    box: tuple[int, int, int, int] | None = None
    #: Unit-length face vector, only when identity is enabled. Never persisted.
    embedding: tuple[float, ...] | None = None


class FaceDetector(Protocol):
    name: str

    def detect(self, frame_data_url: str) -> Detection | None: ...


class FaceEmbedder(Protocol):
    """Turns a detected face into a vector that can be compared with others."""

    name: str

    def embed(self, image: np.ndarray, face: np.ndarray) -> tuple[float, ...] | None: ...


class SFaceEmbedder:
    """OpenCV Zoo SFace: one more ONNX file, no extra Python packages.

    OpenCV ships the recogniser itself, so identification works on the same
    interpreter as the detector. ``alignCrop`` uses the five landmarks YuNet
    already returns, which is what makes the embeddings comparable at all.
    """

    name = "sface"

    def __init__(self, model_path: Path = DEFAULT_SFACE) -> None:
        import cv2

        self.model_path = model_path
        self._recognizer = cv2.FaceRecognizerSF_create(str(model_path), "")
        # cv2 models are not thread-safe, and every participant has its own face
        # loop calling into this object from a worker thread.
        self._lock = threading.Lock()
        log.info("sface ready (%s)", model_path.name)

    def available(self) -> bool:
        return self.model_path.is_file()

    def embed(self, image: np.ndarray, face: np.ndarray) -> tuple[float, ...] | None:
        import numpy as np

        if face is None:
            return None
        try:
            with self._lock:
                aligned = self._recognizer.alignCrop(image, face)
                feature = self._recognizer.feature(aligned).flatten()
        except Exception:  # noqa: BLE001 - a bad crop must not break the meeting
            log.debug("sface: cannot embed this frame", exc_info=True)
            return None
        norm = float(np.linalg.norm(feature))
        if norm <= 1e-9:
            return None
        return tuple(float(value) / norm for value in feature)


class NullDetector:
    """No detector installed: frames pass through unchanged."""

    name = "none"

    def detect(self, frame_data_url: str) -> Detection | None:  # noqa: ARG002
        return None


def face_similarity(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    """Cosine similarity of two embeddings; public so tools and tests can use it."""
    from .identity import cosine

    return cosine(first, second)


def make_embedder() -> FaceEmbedder | None:
    """The identity backend, or None when identity is off (the default)."""
    from .identity import identity_enabled

    if not identity_enabled():
        return None

    path = Path(os.environ.get("TRANSCRIBE_FACE_MODEL", DEFAULT_SFACE))
    if path.is_file():
        try:
            return SFaceEmbedder(path)
        except Exception:  # noqa: BLE001 - a broken model must not stop the meeting
            log.exception("sface: cannot load %s", path)
    else:
        log.warning("face identity is on but %s is missing", path)
    log.warning("face identity is on but no embedding model is available; identity stays off")
    return None


def _decode(frame_data_url: str) -> bytes:
    try:
        return base64.b64decode(frame_data_url.split(",", 1)[-1], validate=True)
    except Exception:  # noqa: BLE001 - a malformed frame is not an error worth raising
        return b""


#: The tile that shows the crop is 4:3, so the crop must be 4:3 too — otherwise
#: CSS crops it again and the mouth disappears. The window below is deliberately
#: wider than the face and shifted down: a face box ends at the chin.
CROP_ASPECT = 4 / 3
CROP_CONTEXT = 2.0  # crop width = 2 face widths
CROP_DOWN_SHIFT = 0.10  # of face height, to keep the mouth and chin


def face_crop(
    image: np.ndarray,
    box: tuple[int, int, int, int],
    *,
    max_width: int = 320,
    aspect: float = CROP_ASPECT,
    context: float = CROP_CONTEXT,
) -> str | None:
    """Cut a pleasant 4:3 portrait around a face and return a JPEG data URL.

    Pure geometry, so it can be tested without a model: the window is clamped to
    the frame instead of being cut off at the edge, and the aspect ratio is kept
    even then, because the UI would otherwise crop the picture a second time.
    """
    import cv2

    height, width = image.shape[:2]
    x, y, w, h = box
    if w <= 0 or h <= 0 or width <= 0 or height <= 0:
        return None
    if x + w <= 0 or y + h <= 0 or x >= width or y >= height:
        return None  # the box is entirely outside the frame

    center_x = x + w / 2
    center_y = y + h / 2 + CROP_DOWN_SHIFT * h

    crop_w = max(w * context, h * 1.6)
    crop_h = crop_w / aspect
    if crop_h < h * 1.5:  # never cut the head to satisfy the aspect ratio
        crop_h = h * 1.5
        crop_w = crop_h * aspect

    crop_w = min(crop_w, width)
    crop_h = min(crop_h, height)
    if crop_h * aspect <= width:  # keep the aspect after clamping
        crop_w = crop_h * aspect
    else:
        crop_h = crop_w / aspect

    left = center_x - crop_w / 2
    top = center_y - crop_h / 2
    left = max(0.0, min(left, width - crop_w))
    top = max(0.0, min(top, height - crop_h))

    x1, y1 = int(round(left)), int(round(top))
    x2, y2 = int(round(left + crop_w)), int(round(top + crop_h))
    crop = image[y1:y2, x1:x2]
    if crop.size == 0:
        return None

    scale = min(1.0, max_width / max(1, crop.shape[1]))
    if scale < 1.0:
        crop = cv2.resize(crop, (int(crop.shape[1] * scale), int(crop.shape[0] * scale)))

    ok, buffer = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode()


class YuNetDetector:
    """OpenCV FaceDetectorYN: fast, tiny, no extra model zoo to manage."""

    name = "yunet"

    def __init__(
        self,
        model_path: Path | str | None = None,
        *,
        max_width: int = 320,
        score_threshold: float = 0.6,
        embedder: FaceEmbedder | None = None,
    ) -> None:
        self.model_path = Path(model_path) if model_path else DEFAULT_YUNET
        self.max_width = max_width
        self.score_threshold = score_threshold
        self.embedder = embedder
        self._detector: Any = None
        #: Every participant has its own face loop, so the model is called from
        #: several threads: cv2 objects are not safe to share without a lock.
        self._lock = threading.Lock()

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
        with self._lock:
            detector = self._ensure()
            detector.setInputSize((width, height))
            _count, faces = detector.detect(image)
        if faces is None or len(faces) == 0:
            return None

        best: tuple[float, tuple[int, int, int, int], Any] | None = None
        for face in faces:
            score = float(face[-1])
            x, y, w, h = (int(value) for value in face[:4])
            if score < self.score_threshold or min(w, h) < MIN_FACE_PX:
                continue
            if best is None or score > best[0]:
                best = (score, (x, y, w, h), face)
        if best is None:
            return None

        score, box, face_row = best
        thumbnail = face_crop(image, box, max_width=self.max_width)
        if thumbnail is None:
            return None
        embedding = None
        if self.embedder is not None:
            try:
                embedding = self.embedder.embed(image, face_row)
            except Exception:  # noqa: BLE001 - identity must never break a meeting
                log.debug("sface: embedding failed", exc_info=True)
        return Detection(
            confidence=round(score, 3),
            thumbnail=thumbnail,
            box=box,
            embedding=embedding,
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
        thumbnail = face_crop(image, (x1, y1, x2 - x1, y2 - y1), max_width=self.max_width)
        if thumbnail is None:
            return None
        return Detection(
            confidence=round(score, 3),
            thumbnail=thumbnail,
            box=(x1, y1, x2 - x1, y2 - y1),
        )


def make_detector(kind: str = "auto") -> FaceDetector:
    """Pick the best detector that actually works on this interpreter."""
    embedder = make_embedder()
    if kind in ("auto", "insightface"):
        try:
            import insightface  # noqa: F401
            import onnxruntime  # noqa: F401

            return InsightFaceDetector()
        except Exception:  # noqa: BLE001 - expected on Python 3.14
            if kind == "insightface":
                log.warning("insightface requested but unavailable")

    if kind in ("auto", "yunet"):
        candidate = YuNetDetector(embedder=embedder)
        if candidate.available():
            try:
                import cv2  # noqa: F401

                return candidate
            except Exception:  # noqa: BLE001
                log.warning("opencv missing, face detection disabled")

    return NullDetector()
