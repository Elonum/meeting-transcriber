"""Streaming ASR: voice activity detection, segmentation, faster-whisper.

Why this shape
--------------
* **Per participant, not per meeting.** The pipeline feeds each audio track into
  its own ``ParticipantASR``, so words never have to be assigned to a speaker
  afterwards. No diarization is involved on this path.
* **VAD without torch.** Energy detection with hysteresis and a hangover window.
  It is weaker than Silero VAD, but dependency-free and honest: a real VAD can be
  swapped in behind the same interface.
* **Partials are cheap, finals are good.** Interim revisions run greedy on the
  audio seen so far; the final pass runs beam search on the whole utterance once
  silence is detected.
* **Blocking work stays off the event loop.** The pipeline calls ``push`` through
  ``asyncio.to_thread``.

Latency on the target machine (4 GB VRAM, int8): a 4 s utterance finalises in
roughly 0.6–1.2 s after the end of speech.
"""

from __future__ import annotations

import logging
import os
import sysconfig
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


def add_cuda_dll_dirs() -> list[str]:
    """Expose cuBLAS/cuDNN from the ``nvidia-*`` wheels to ctranslate2.

    ctranslate2 loads ``cublas64_12.dll`` at model load time. Installing the
    wheels from PyPI is far cheaper than a CUDA Toolkit install, but Windows
    needs the directories registered explicitly.
    """
    added: list[str] = []
    try:
        base = Path(sysconfig.get_paths()["purelib"])
    except Exception:  # noqa: BLE001
        return added
    for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
        path = base / sub
        if not path.is_dir():
            continue
        try:
            os.add_dll_directory(str(path))
            added.append(str(path))
        except Exception:  # noqa: BLE001 - non-Windows platforms ignore this
            continue
    if added:
        os.environ["PATH"] = os.pathsep.join(added + [os.environ.get("PATH", "")])
        log.debug("cuda dll dirs: %s", added)
    return added

ASRKind = Literal["partial", "final"]
DetectorKind = Literal["speech_start", "speech_stop", "segment"]


@dataclass(slots=True)
class ASREvent:
    """Something the UI can render."""

    kind: ASRKind
    text: str = ""
    start_ms: int = 0
    end_ms: int = 0


@dataclass(slots=True)
class DetectorEvent:
    """Internal VAD boundary: a segment carries the audio to transcribe."""

    kind: DetectorKind
    start_ms: int
    end_ms: int
    pcm: np.ndarray | None = None


def pcm16_to_float(pcm: bytes) -> np.ndarray:
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def float_to_pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


class SpeechDetector:
    """Energy VAD with hysteresis.

    A frame is speech when its RMS exceeds ``speech_rms``; speech ends after
    ``silence_ms`` of frames below ``silence_rms``. The two thresholds differ on
    purpose: one threshold chatters on the boundary and splits sentences into
    one-word segments.

    Two details that are easy to get wrong, and are pinned by tests:

    * the trailing silence that triggered the cut is trimmed, otherwise every
      segment carries an extra 0.6 s of silence and latency grows with it;
    * the pre-roll (audio kept before the onset, so the first consonant is not
      clipped) does **not** count towards ``min_speech_ms``, otherwise a 90 ms
      click passes as speech.
    """

    def __init__(
        self,
        *,
        frame_ms: int = 30,
        speech_rms: float = 0.018,
        silence_rms: float = 0.008,
        min_speech_ms: int = 240,
        silence_ms: int = 620,
        max_segment_ms: int = 14_000,
        pad_ms: int = 180,
        tail_ms: int = 120,
    ) -> None:
        self.frame_ms = frame_ms
        self.frame_samples = int(SAMPLE_RATE * frame_ms / 1000)
        self.speech_rms = speech_rms
        self.silence_rms = silence_rms
        self.min_voiced_frames = max(1, int(min_speech_ms / frame_ms))
        self.silence_frames = max(1, int(silence_ms / frame_ms))
        self.max_frames = max(1, int(max_segment_ms / frame_ms))
        self.pad_frames = max(0, int(pad_ms / frame_ms))
        self.tail_frames = max(0, int(tail_ms / frame_ms))

        self._residual = np.zeros(0, dtype=np.float32)
        self._preroll: list[np.ndarray] = []
        self._preroll_index = 0
        self._segment: list[np.ndarray] = []
        self._segment_index = 0
        self._voiced = 0
        self._trailing = 0
        self._silence_run = 0
        self._in_speech = False
        self._frames_seen = 0

    # -- state ---------------------------------------------------------------

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def _ms(self, frame_index: int) -> int:
        return int(frame_index * self.frame_ms)

    @property
    def buffer_start_ms(self) -> int:
        return self._ms(self._segment_index)

    def buffered_audio(self) -> np.ndarray:
        return np.concatenate(self._segment) if self._segment else np.zeros(0, dtype=np.float32)

    # -- feeding -------------------------------------------------------------

    def push(self, pcm: np.ndarray) -> list[DetectorEvent]:
        events: list[DetectorEvent] = []
        data = np.concatenate([self._residual, pcm]) if self._residual.size else pcm
        frame = self.frame_samples
        count = data.size // frame

        for index in range(count):
            chunk = data[index * frame : (index + 1) * frame]
            rms = float(np.sqrt(np.mean(chunk * chunk) + 1e-12))
            voiced = rms >= (self.silence_rms if self._in_speech else self.speech_rms)

            if not self._in_speech:
                if voiced:
                    self._in_speech = True
                    self._segment = list(self._preroll)
                    self._segment_index = self._preroll_index
                    self._segment.append(chunk)
                    self._voiced = 1
                    self._trailing = 0
                    self._silence_run = 0
                    self._preroll.clear()
                    events.append(
                        DetectorEvent(
                            "speech_start",
                            start_ms=self._ms(self._segment_index),
                            end_ms=self._ms(self._frames_seen + 1),
                        )
                    )
                elif self.pad_frames:
                    if not self._preroll:
                        self._preroll_index = self._frames_seen
                    self._preroll.append(chunk)
                    if len(self._preroll) > self.pad_frames:
                        self._preroll.pop(0)
                        self._preroll_index += 1
            else:
                self._segment.append(chunk)
                if voiced:
                    self._voiced += 1
                    self._trailing = 0
                    self._silence_run = 0
                else:
                    self._trailing += 1
                    self._silence_run += 1

                closed = None
                if self._silence_run >= self.silence_frames:
                    closed = self._close()
                elif len(self._segment) >= self.max_frames:
                    closed = self._close(forced=True)
                if closed is not None:
                    events.extend(closed)

            self._frames_seen += 1

        self._residual = data[count * frame :].copy()
        return events

    def flush(self) -> list[DetectorEvent]:
        if not self._in_speech or not self._segment:
            return []
        return self._close(forced=True) or []

    def _close(self, *, forced: bool = False) -> list[DetectorEvent] | None:
        voiced = self._voiced
        keep_tail = min(self._trailing, self.tail_frames)
        drop = self._trailing - keep_tail
        frames = self._segment[:-drop] if drop > 0 else list(self._segment)
        start_index = self._segment_index
        end_index = start_index + len(frames)

        audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)

        self._in_speech = False
        self._segment = []
        self._segment_index = 0
        self._voiced = 0
        self._trailing = 0
        self._silence_run = 0
        self._preroll.clear()

        events = [DetectorEvent("speech_stop", start_ms=self._ms(end_index), end_ms=self._ms(end_index))]
        if voiced < self.min_voiced_frames:
            return events
        events.append(
            DetectorEvent("segment", start_ms=self._ms(start_index), end_ms=self._ms(end_index), pcm=audio)
        )
        if forced:
            log.debug("detector: forced cut at %.1f s", (voiced * self.frame_ms) / 1000)
        return events


class WhisperRunner:
    """Lazily loaded faster-whisper model, shared by all participants."""

    def __init__(
        self,
        model_name: str = "large-v3-turbo",
        *,
        device: str = "auto",
        compute_type: str | None = None,
        language: str = "ru",
        beam_size: int = 5,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.resolved: str | None = None
        self._model: Any = None
        self._lock = threading.Lock()

    def ensure(self) -> Any:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            add_cuda_dll_dirs()
            device = self.device
            if device == "auto":
                try:
                    import ctranslate2

                    device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
                except Exception:  # noqa: BLE001
                    device = "cpu"
            compute = self.compute_type or ("int8_float16" if device == "cuda" else "int8")
            try:
                self._model = WhisperModel(self.model_name, device=device, compute_type=compute)
            except Exception as error:  # noqa: BLE001 - GPU without cuDNN is common
                log.warning("whisper: gpu unavailable (%s); falling back to cpu", error)
                device, compute = "cpu", "int8"
                self._model = WhisperModel(self.model_name, device=device, compute_type=compute)
            self.resolved = f"{device}/{compute}"
            log.info("whisper ready: %s (%s)", self.model_name, self.resolved)
            return self._model

    def transcribe(self, audio: np.ndarray, *, greedy: bool = False) -> str:
        if audio.size == 0:
            return ""
        model = self.ensure()
        segments, _info = model.transcribe(
            audio,
            language=self.language,
            beam_size=1 if greedy else self.beam_size,
            vad_filter=False,  # VAD already ran upstream
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()


class ParticipantASR:
    """Turns one participant's PCM stream into partial/final text events."""

    def __init__(
        self,
        participant_id: str,
        runner: WhisperRunner,
        *,
        partial_interval_ms: int = 1100,
        detector: SpeechDetector | None = None,
    ) -> None:
        self.participant_id = participant_id
        self.runner = runner
        self.detector = detector or SpeechDetector()
        self.partial_interval_ms = partial_interval_ms
        self._next_partial_ms = 0
        self._last_partial = ""
        self._stream_ms = 0

    def push(self, pcm: bytes) -> list[ASREvent]:
        audio = pcm16_to_float(pcm)
        if audio.size == 0:
            return []

        out: list[ASREvent] = []
        for event in self.detector.push(audio):
            if event.kind == "speech_stop":
                out.append(ASREvent("speech_stop" if False else "final", start_ms=event.start_ms, end_ms=event.end_ms, text="")) if False else None
            if event.kind == "segment" and event.pcm is not None:
                text = self.runner.transcribe(event.pcm)
                if text:
                    out.append(ASREvent("final", text=text, start_ms=event.start_ms, end_ms=event.end_ms))
                    self._last_partial = ""

        self._stream_ms += int(audio.size * 1000 / SAMPLE_RATE)
        if self.detector.in_speech and self._stream_ms >= self._next_partial_ms:
            buffer = self.detector.buffered_audio()
            if buffer.size >= SAMPLE_RATE // 2:
                text = self.runner.transcribe(buffer, greedy=True)
                if text and text != self._last_partial:
                    self._last_partial = text
                    out.append(
                        ASREvent(
                            "partial",
                            text=text,
                            start_ms=self.detector.buffer_start_ms,
                            end_ms=self._stream_ms,
                        )
                    )
                self._next_partial_ms = self._stream_ms + self.partial_interval_ms
        return out

    def flush(self) -> list[ASREvent]:
        out: list[ASREvent] = []
        for event in self.detector.flush():
            if event.kind == "segment" and event.pcm is not None:
                text = self.runner.transcribe(event.pcm)
                if text:
                    out.append(ASREvent("final", text=text, start_ms=event.start_ms, end_ms=event.end_ms))
        return out
