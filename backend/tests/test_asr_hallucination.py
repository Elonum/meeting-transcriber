"""No invented text: keyboard clicks and music must not become a transcript.

The reported symptom: the microphone was on, nobody spoke, the keyboard clicked —
and the transcript said "Продолжение следует…", "Смотрите продолжение в
следующей серии" and "ご視聴ありがとうございました". Whisper was trained on
subtitled video and "remembers" closing credits when the audio is not speech.

Two layers stop it: the detector refuses impulsive bursts, and the runner drops
segments that the model itself marks as guesses.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
from app.asr import (
    DEFAULT_PROMPT,
    HALLUCINATION_PHRASES,
    SAMPLE_RATE,
    SpeechDetector,
    WhisperRunner,
    looks_like_hallucination,
    looks_like_prompt_echo,
    normalize_text,
    prompt_tokens,
    segment_is_noise,
)

FRAME = 480  # 30 ms at 16 kHz


def segment(text: str, *, no_speech: float = 0.1, logprob: float = -0.3, compression: float = 1.2) -> Any:
    return SimpleNamespace(
        text=text,
        no_speech_prob=no_speech,
        avg_logprob=logprob,
        compression_ratio=compression,
    )


def runner_with(segments: list[Any], prompt: str | None = DEFAULT_PROMPT) -> WhisperRunner:
    class Model:
        def transcribe(self, audio, **kwargs):  # noqa: ANN001, ARG002
            return iter(segments), SimpleNamespace(language="ru", language_probability=0.9)

    runner = WhisperRunner("tiny", prompt=prompt)
    runner._model = Model()
    runner.resolved = "fake"
    return runner


def loud(frames: int, amplitude: float = 0.35) -> np.ndarray:
    t = np.arange(FRAME * frames) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(frames: int) -> np.ndarray:
    return np.zeros(FRAME * frames, dtype=np.float32)


# -- text level -------------------------------------------------------------


def test_stock_subtitle_credits_are_all_recognised() -> None:
    for phrase in HALLUCINATION_PHRASES:
        assert looks_like_hallucination(phrase), phrase


def test_normal_speech_is_not_flagged() -> None:
    for text in ("Обсудим дедлайн завтра.", "Спасибо, коллеги!", "Let's ship it on Friday."):
        assert not looks_like_hallucination(text), text


def test_punctuation_only_is_dropped() -> None:
    for text in ("...", "—", "   ", ""):
        assert looks_like_hallucination(text), text


def test_normalisation_ignores_case_and_punctuation() -> None:
    assert normalize_text("  Продолжение   СЛЕДУЕТ… ") == "продолжение следует"


# -- segment statistics -----------------------------------------------------


def test_confident_segment_is_noise_free() -> None:
    assert not segment_is_noise(segment("Привет"))


def test_no_speech_probability_marks_a_guess() -> None:
    assert segment_is_noise(segment("Привет", no_speech=0.9))


def test_low_logprob_marks_a_guess() -> None:
    assert segment_is_noise(segment("Привет", logprob=-2.5))


def test_repetitive_text_marks_a_guess() -> None:
    assert segment_is_noise(segment("Привет", compression=3.5))


# -- runner integration -----------------------------------------------------


def test_credits_do_not_reach_the_transcript() -> None:
    runner = runner_with(
        [
            segment("Продолжение следует..."),
            segment("Смотрите продолжение в следующей серии."),
            segment("ご視聴ありがとうございました"),
            segment("Звучит музыка."),
        ]
    )

    result = runner.transcribe(np.zeros(16_000, dtype=np.float32))

    assert result.text == "", result.text


def test_a_real_phrase_next_to_credits_survives() -> None:
    runner = runner_with(
        [
            segment("Продолжение следует..."),
            segment("Созвон переносим на четверг."),
            segment("Звучит музыка."),
        ]
    )

    result = runner.transcribe(np.zeros(16_000, dtype=np.float32))

    assert result.text == "Созвон переносим на четверг."


def test_guesses_by_statistics_are_dropped() -> None:
    runner = runner_with(
        [
            segment("Привет, как дела", no_speech=0.95),
            segment("Обсудим релиз", logprob=-2.0),
        ]
    )

    assert runner.transcribe(np.zeros(16_000, dtype=np.float32)).text == ""


# -- detector level ---------------------------------------------------------


def test_keyboard_clicks_do_not_open_a_segment() -> None:
    """Many loud single frames used to add up to "enough speech" for Whisper."""
    detector = SpeechDetector()
    events = []
    for _ in range(14):  # 14 clicks, 10 frames apart, well above speech_rms
        events.extend(detector.push(loud(1)))
        events.extend(detector.push(silence(9)))
    events.extend(detector.push(silence(30)))

    assert [event.kind for event in events if event.kind == "segment"] == []


def test_sustained_speech_still_opens_a_segment() -> None:
    detector = SpeechDetector()
    events = detector.push(loud(40))
    events.extend(detector.push(silence(30)))

    segments = [event for event in events if event.kind == "segment"]
    assert len(segments) == 1
    assert segments[0].pcm is not None and segments[0].pcm.size > 0


def test_a_short_click_inside_speech_does_not_break_the_segment() -> None:
    detector = SpeechDetector()
    events = detector.push(loud(30))
    events.extend(detector.push(silence(4)))
    events.extend(detector.push(loud(20)))
    events.extend(detector.push(silence(30)))

    assert len([event for event in events if event.kind == "segment"]) == 1


def test_clicks_after_real_speech_do_not_form_a_second_segment() -> None:
    detector = SpeechDetector()
    detector.push(loud(30))
    detector.push(silence(30))  # closes the real phrase
    events = []
    for _ in range(6):
        events.extend(detector.push(loud(1)))
        events.extend(detector.push(silence(9)))
    events.extend(detector.push(silence(30)))

    assert [event.kind for event in events if event.kind == "segment"] == []


# -- prompt echo ------------------------------------------------------------


def test_prompt_echo_is_detected() -> None:
    tokens = prompt_tokens(DEFAULT_PROMPT)

    assert looks_like_prompt_echo("Jazz, Jazz", tokens)
    assert looks_like_prompt_echo("SaluteJazz", tokens)
    assert looks_like_prompt_echo("транскрибация", tokens)


def test_real_speech_with_a_glossary_word_is_kept() -> None:
    tokens = prompt_tokens(DEFAULT_PROMPT)

    assert not looks_like_prompt_echo("Сегодня обсуждаем транскрибацию встреч в Jazz", tokens)
    assert not looks_like_prompt_echo("Привет, коллеги", tokens)
    assert not looks_like_prompt_echo("Дедлайн переносим на пятницу", tokens)


def test_runner_drops_prompt_echo() -> None:
    runner = runner_with([segment("Jazz, Jazz")])

    assert runner.transcribe(np.zeros(16_000, dtype=np.float32)).text == ""
