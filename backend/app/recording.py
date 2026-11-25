"""Audio recording: one WAV per participant, written as the meeting runs.

Why per participant
-------------------
The agent already receives a separate audio track per participant, so recording
them apart is both simpler and more useful than mixing: the transcript can point
at ``p-3.wav`` and the file matches what that person said. Mixing would need
synchronisation and would clip on overlapping speech.

Format
------
16 kHz, mono, 16-bit PCM — exactly what the browser sends and what Whisper
consumes, so no resampling step exists to go wrong.

Safety and limits
-----------------
* every write is guarded by a lock: the pipeline may feed several tracks from
  different worker threads;
* a session cannot grow past ``max_bytes`` (default 1 GB); when it does, the
  recorder stops writing once and says so in the log instead of filling the disk;
* a broken file handle never kills the meeting: the error is logged and that
  track simply stops recording.
"""

from __future__ import annotations

import logging
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # int16
CHANNELS = 1

#: 1 GB of PCM16 mono at 16 kHz is about 9 hours of audio.
DEFAULT_MAX_BYTES = 1_000_000_000


@dataclass(slots=True)
class RecordingInfo:
    participant_id: str
    file: str
    bytes: int

    @property
    def duration_s(self) -> float:
        return round(self.bytes / (SAMPLE_RATE * SAMPLE_WIDTH), 1)


class TrackRecorder:
    """A single participant's WAV file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.bytes = 0
        self._closed = False
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        # The handle stays open for the whole track on purpose: a context
        # manager would reopen the file for every audio chunk.
        self._wave = wave.open(str(path), "wb")  # noqa: SIM115
        self._wave.setnchannels(CHANNELS)
        self._wave.setsampwidth(SAMPLE_WIDTH)
        self._wave.setframerate(SAMPLE_RATE)

    def write(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        with self._lock:
            try:
                self._wave.writeframes(pcm)
                self.bytes += len(pcm)
            except Exception:  # noqa: BLE001 - a bad write must not stop the meeting
                log.exception("recording: write failed for %s", self.path.name)
                self._closed = True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._wave.close()
            except Exception:  # noqa: BLE001
                log.exception("recording: close failed for %s", self.path.name)


class SessionRecorder:
    """All participant files of one session, under ``<session>/audio``."""

    def __init__(
        self,
        directory: Path,
        *,
        enabled: bool = True,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.directory = directory
        self.enabled = enabled
        self.max_bytes = max_bytes
        self._tracks: dict[str, TrackRecorder] = {}
        self._lock = threading.Lock()
        self._total = 0
        self._limit_logged = False

    def add(self, participant_id: str, pcm: bytes) -> None:
        if not self.enabled or not pcm:
            return
        with self._lock:
            if self._total >= self.max_bytes:
                if not self._limit_logged:
                    log.warning("recording: session limit of %d bytes reached, stopping", self.max_bytes)
                    self._limit_logged = True
                return
            recorder = self._tracks.get(participant_id)
            if recorder is None:
                safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in participant_id)
                recorder = TrackRecorder(self.directory / f"{safe}.wav")
                self._tracks[participant_id] = recorder
            recorder.write(pcm)
            self._total += len(pcm)

    def close(self) -> None:
        with self._lock:
            for recorder in self._tracks.values():
                recorder.close()
            self._tracks.clear()

    def manifest(self) -> list[RecordingInfo]:
        return scan_audio_dir(self.directory)

    @property
    def total_bytes(self) -> int:
        return self._total


def scan_audio_dir(directory: Path) -> list[RecordingInfo]:
    """List WAV files with their payload size (the 44-byte header excluded)."""
    if not directory.is_dir():
        return []
    items: list[RecordingInfo] = []
    for path in sorted(directory.glob("*.wav")):
        try:
            size = max(0, path.stat().st_size - 44)
        except OSError:  # noqa: PERF203 - a race with deletion is not an error
            continue
        items.append(RecordingInfo(participant_id=path.stem, file=path.name, bytes=size))
    return items


def read_recordings(session_dir: Path) -> list[RecordingInfo]:
    """Manifest for a session directory, straight from disk."""
    return scan_audio_dir(session_dir / "audio")
