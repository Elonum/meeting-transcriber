"""Recording tests: real WAV files, read back with the standard library.

No audio hardware and no Whisper: the recorder is fed synthetic PCM and the files
are validated by parsing them, which is what a player or an editor would do.
"""

from __future__ import annotations

import wave

import numpy as np
import pytest

from app.recording import (
    SAMPLE_RATE,
    SessionRecorder,
    TrackRecorder,
    read_recordings,
    scan_audio_dir,
)


def pcm(ms: int, amplitude: float = 0.2) -> bytes:
    samples = int(SAMPLE_RATE * ms / 1000)
    tone = amplitude * np.sin(2 * np.pi * 220 * np.arange(samples) / SAMPLE_RATE)
    return (tone * 32767).astype(np.int16).tobytes()


def read_wav(path):
    with wave.open(str(path), "rb") as handle:
        return (
            handle.getnchannels(),
            handle.getsampwidth(),
            handle.getframerate(),
            handle.getnframes(),
        )


def test_track_recorder_writes_a_valid_wav(tmp_path) -> None:
    recorder = TrackRecorder(tmp_path / "p-1.wav")
    recorder.write(pcm(500))
    recorder.write(pcm(500))
    recorder.close()

    channels, width, rate, frames = read_wav(tmp_path / "p-1.wav")
    assert (channels, width, rate) == (1, 2, SAMPLE_RATE)
    # 1 second of 16 kHz mono int16.
    assert frames == SAMPLE_RATE
    assert recorder.bytes == SAMPLE_RATE * 2


def test_close_is_idempotent(tmp_path) -> None:
    recorder = TrackRecorder(tmp_path / "p-2.wav")
    recorder.write(pcm(100))
    recorder.close()
    recorder.close()  # must not raise
    assert read_wav(tmp_path / "p-2.wav")[3] == SAMPLE_RATE // 10


def test_write_after_close_is_ignored(tmp_path) -> None:
    recorder = TrackRecorder(tmp_path / "p-3.wav")
    recorder.close()
    recorder.write(pcm(100))
    assert recorder.bytes == 0


def test_session_recorder_splits_participants(tmp_path) -> None:
    session = SessionRecorder(tmp_path / "audio")
    session.add("p-1", pcm(300))
    session.add("p-2", pcm(600))
    session.add("p-1", pcm(200))
    session.close()

    assert read_wav(tmp_path / "audio" / "p-1.wav")[3] == int(SAMPLE_RATE * 0.5)
    assert read_wav(tmp_path / "audio" / "p-2.wav")[3] == int(SAMPLE_RATE * 0.6)

    manifest = session.manifest()
    assert [item.participant_id for item in manifest] == ["p-1", "p-2"]
    assert manifest[0].duration_s == pytest.approx(0.5, abs=0.05)


def test_participant_ids_are_sanitised(tmp_path) -> None:
    session = SessionRecorder(tmp_path / "audio")
    session.add("../../etc/passwd", pcm(100))
    session.close()

    files = list((tmp_path / "audio").glob("*.wav"))
    assert len(files) == 1
    assert ".." not in files[0].name and "/" not in files[0].name


def test_disabled_recorder_writes_nothing(tmp_path) -> None:
    session = SessionRecorder(tmp_path / "audio", enabled=False)
    session.add("p-1", pcm(200))
    session.close()

    assert not (tmp_path / "audio").exists()
    assert session.manifest() == []


def test_size_limit_stops_writing(tmp_path) -> None:
    session = SessionRecorder(tmp_path / "audio", max_bytes=10_000)
    for _ in range(10):
        session.add("p-1", pcm(200))  # 6400 bytes each
    session.close()

    written = (tmp_path / "audio" / "p-1.wav").stat().st_size
    # The first write crosses the limit; nothing after it may be appended.
    assert written <= 10_000 + 6_400


def test_scan_audio_dir_reports_payload_size(tmp_path) -> None:
    directory = tmp_path / "audio"
    directory.mkdir()
    recorder = TrackRecorder(directory / "p-5.wav")
    recorder.write(pcm(400))
    recorder.close()

    items = scan_audio_dir(directory)
    assert len(items) == 1
    assert items[0].file == "p-5.wav"
    assert items[0].bytes == int(SAMPLE_RATE * 0.4) * 2
    assert items[0].duration_s == pytest.approx(0.4, abs=0.05)


def test_read_recordings_missing_directory(tmp_path) -> None:
    assert read_recordings(tmp_path / "nope") == []
