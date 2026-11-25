"""Unit tests for the VAD/segmentation layer.

No Whisper, no browser: synthetic audio only, so these run in milliseconds and
pin down the behaviour the pipeline depends on.
"""

from __future__ import annotations

import numpy as np
import pytest
from app.asr import ParticipantASR, SpeechDetector, Transcript, float_to_pcm16, pcm16_to_float

RATE = 16_000


def silence(ms: int) -> np.ndarray:
    return np.zeros(int(RATE * ms / 1000), dtype=np.float32)


def tone(ms: int, freq: float = 220.0, amplitude: float = 0.3) -> np.ndarray:
    samples = int(RATE * ms / 1000)
    t = np.arange(samples, dtype=np.float32) / RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def segments(events):
    return [event for event in events if event.kind == "segment"]


def test_silence_produces_no_segments() -> None:
    detector = SpeechDetector()
    events = detector.push(silence(2000))
    assert segments(events) == []
    assert detector.in_speech is False


def test_single_burst_is_one_segment() -> None:
    detector = SpeechDetector()
    events = detector.push(silence(400)) + detector.push(tone(1200)) + detector.push(silence(900))
    found = segments(events)
    assert len(found) == 1, [e.kind for e in events]
    segment = found[0]
    assert segment.pcm is not None
    # 1.2 s of speech, 180 ms pre-roll, 120 ms tail kept; the 620 ms hangover
    # silence must be trimmed or latency grows with every phrase.
    assert 1.35 <= segment.pcm.size / RATE <= 1.65, segment.pcm.size / RATE
    assert segment.start_ms >= 0
    assert segment.end_ms > segment.start_ms


def test_two_bursts_are_two_segments() -> None:
    detector = SpeechDetector()
    events = (
        detector.push(tone(700))
        + detector.push(silence(1000))
        + detector.push(tone(700))
        + detector.push(silence(900))
    )
    found = segments(events)
    assert len(found) == 2, [e.kind for e in events]
    assert found[0].end_ms < found[1].start_ms


def test_short_click_is_dropped() -> None:
    detector = SpeechDetector()
    events = detector.push(tone(90)) + detector.push(silence(900))
    assert segments(events) == []


def test_long_speech_is_force_cut() -> None:
    detector = SpeechDetector(max_segment_ms=3000)
    events = detector.push(tone(8000))
    found = segments(events)
    assert len(found) >= 2, [e.kind for e in events]
    assert all(segment.pcm is not None and segment.pcm.size / RATE <= 3.2 for segment in found)


def test_flush_closes_open_segment() -> None:
    detector = SpeechDetector()
    detector.push(tone(1500))
    assert detector.in_speech is True
    events = detector.flush()
    assert len(segments(events)) == 1
    assert detector.in_speech is False


def test_pcm_roundtrip() -> None:
    original = tone(120, amplitude=0.5)
    restored = pcm16_to_float(float_to_pcm16(original))
    assert restored.size == original.size
    assert np.max(np.abs(restored - original)) < 1e-3


class FakeRunner:
    """Stands in for Whisper so the event logic is tested without a model."""

    language: str | None = None  # auto-detection, like a default WhisperRunner

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def transcribe(
        self, audio: np.ndarray, *, greedy: bool = False, language: str | None = None
    ) -> Transcript:
        self.calls.append(greedy)
        return Transcript(text="проверочный текст", language="ru")


def test_partial_then_final() -> None:
    runner = FakeRunner()
    asr = ParticipantASR("p-1", runner, partial_interval_ms=500)  # type: ignore[arg-type]

    events = []
    # 2 s of speech in 100 ms packets: partials must appear before the final.
    for _ in range(20):
        events.extend(asr.push(float_to_pcm16(tone(100))))
    assert any(event.kind == "partial" for event in events)

    events.extend(asr.push(float_to_pcm16(silence(900))))
    finals = [event for event in events if event.kind == "final"]
    assert len(finals) == 1
    assert finals[0].text == "проверочный текст"
    # A partial pass must be greedy, the final one must use the beam.
    assert runner.calls[-1] is False
    assert any(call is True for call in runner.calls)


@pytest.mark.parametrize("packet_ms", [20, 100, 500])
def test_packetisation_does_not_lose_audio(packet_ms: int) -> None:
    detector = SpeechDetector()
    audio = np.concatenate([silence(300), tone(1000), silence(800)])
    step = int(RATE * packet_ms / 1000)
    events = []
    for index in range(0, audio.size, step):
        events.extend(detector.push(audio[index : index + step]))
    found = segments(events)
    assert len(found) == 1
    assert found[0].pcm is not None
    assert found[0].pcm.size / RATE == pytest.approx(1.30, abs=0.15)
