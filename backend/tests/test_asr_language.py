"""Language handling: English must stay English.

The bug this covers: with a forced ``language="ru"`` Whisper rewrote English
speech as Russian words, which silently destroys the meaning of a mixed meeting.
"""

from __future__ import annotations

import numpy as np
from app.asr import SAMPLE_RATE, ASREvent, ParticipantASR, Transcript, WhisperRunner


class FakeRunner:
    """Stands in for Whisper: records what language it was asked for."""

    def __init__(
        self, language: str | None = None, detected: str = "en", probability: float = 0.98
    ) -> None:
        self.language = language  # pinned language, like WhisperRunner.language
        self.detected = detected
        self.probability = probability
        self.calls: list[str | None] = []

    def transcribe(self, audio, *, greedy: bool = False, language=None) -> Transcript:  # noqa: ANN001, ARG002
        self.calls.append(language)
        return Transcript(text="hello there", language=self.detected, probability=self.probability)


def audio(seconds: float) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)


def test_auto_mode_lets_whisper_decide_on_a_full_phrase() -> None:
    runner = FakeRunner()
    asr = ParticipantASR("p-1", runner)  # type: ignore[arg-type]

    asr._transcribe(audio(2.0))

    assert runner.calls == [None], "a long phrase must be auto-detected, not forced to ru"


def test_detected_language_sticks_for_short_utterances() -> None:
    """A one-word "да" must not make Whisper guess again and flip the language."""
    runner = FakeRunner(detected="en")
    asr = ParticipantASR("p-1", runner)  # type: ignore[arg-type]

    asr._transcribe(audio(2.0))  # detects en
    asr._transcribe(audio(0.4))  # too short to detect reliably

    assert runner.calls == [None, "en"]


def test_short_first_utterance_is_still_auto() -> None:
    runner = FakeRunner()
    asr = ParticipantASR("p-1", runner)  # type: ignore[arg-type]

    asr._transcribe(audio(0.3))

    assert runner.calls == [None]


def test_pinned_language_wins_over_detection() -> None:
    runner = FakeRunner(language="ru")
    asr = ParticipantASR("p-1", runner)  # type: ignore[arg-type]

    asr._transcribe(audio(3.0))
    asr._transcribe(audio(0.3))

    assert runner.calls == ["ru", "ru"]


def test_detection_does_not_override_a_pinned_language() -> None:
    runner = FakeRunner(language="ru", detected="en")
    asr = ParticipantASR("p-1", runner)  # type: ignore[arg-type]

    asr._transcribe(audio(3.0))

    assert asr.language is None, "the operator's choice must not be overwritten"


def test_russian_speech_stays_russian() -> None:
    runner = FakeRunner(detected="ru")
    asr = ParticipantASR("p-1", runner)  # type: ignore[arg-type]

    events = asr.push(b"\x00\x00" * 1000)

    assert all(event.language in (None, "ru") for event in events)


def test_segment_carries_the_detected_language() -> None:
    """The UI shows a tag for non-Russian phrases, so the field must reach it."""
    event = ASREvent(kind="final", text="hello", language="en")
    assert event.language == "en"

    default = ASREvent(kind="final", text="привет")
    assert default.language is None


def test_runner_exposes_the_detected_language() -> None:
    """WhisperRunner must pass the language through, not just the text."""
    from types import SimpleNamespace

    class Model:
        def transcribe(self, audio, **kwargs):  # noqa: ANN001, ARG002
            return iter([SimpleNamespace(text=" hello ")]), SimpleNamespace(
                language="en", language_probability=0.97
            )

    runner = WhisperRunner("tiny")
    runner._model = Model()
    runner.resolved = "fake"

    result = runner.transcribe(audio(1.0))

    assert result.text == "hello"
    assert result.language == "en"
    assert result.probability == 0.97


def test_glossary_prompt_reaches_the_model() -> None:
    """The prompt is what turned "Джесс" into "Jazz"; it must be passed through."""
    from types import SimpleNamespace

    class Model:
        def __init__(self) -> None:
            self.kwargs: dict = {}

        def transcribe(self, audio, **kwargs):  # noqa: ANN001, ARG002
            self.kwargs = kwargs
            return iter([SimpleNamespace(text=" Jazz ")]), SimpleNamespace(language="ru")

    model = Model()
    runner = WhisperRunner("tiny", prompt="SaluteJazz, Jazz")
    runner._model = model

    runner.transcribe(audio(1.0))

    assert model.kwargs["initial_prompt"] == "SaluteJazz, Jazz"


def test_prompt_can_be_disabled_by_an_empty_env(monkeypatch) -> None:
    from app.asr import DEFAULT_PROMPT, prompt_from_env

    monkeypatch.delenv("TRANSCRIBE_PROMPT", raising=False)
    assert prompt_from_env() == DEFAULT_PROMPT

    monkeypatch.setenv("TRANSCRIBE_PROMPT", "  ")
    assert prompt_from_env() is None

    monkeypatch.setenv("TRANSCRIBE_PROMPT", "свои, термины")
    assert prompt_from_env() == "свои, термины"


def test_runner_cache_separates_prompts() -> None:
    from app.asr import get_runner

    first = get_runner("tiny", language=None, prompt="a")
    second = get_runner("tiny", language=None, prompt="b")
    again = get_runner("tiny", language=None, prompt="a")

    assert first is again
    assert first is not second


def test_a_short_russian_phrase_is_not_tagged_english() -> None:
    """The reported bug: "Всем привет!" (1.3 s) came back labelled EN."""
    runner = FakeRunner(detected="ru")
    asr = ParticipantASR("p-1", runner)  # type: ignore[arg-type]
    asr._transcribe(audio(2.0))  # establishes ru as the sticky language

    runner.detected = "en"  # Whisper's guess on one short utterance
    result = asr._transcribe(audio(1.3))

    assert runner.calls[-1] == "ru", "a short phrase must be decoded with the known language"
    assert result.language == "ru", "and must be tagged with it, not with the guess"
    assert asr.language == "ru"


def test_a_long_confident_utterance_switches_the_language() -> None:
    runner = FakeRunner(detected="ru")
    asr = ParticipantASR("p-1", runner)  # type: ignore[arg-type]
    asr._transcribe(audio(2.0))

    runner.detected = "en"
    runner.probability = 0.9
    result = asr._transcribe(audio(3.0))

    assert runner.calls[-1] is None, "a long utterance deserves its own detection"
    assert result.language == "en"
    assert asr.language == "en"


def test_a_long_but_unsure_utterance_does_not_switch() -> None:
    runner = FakeRunner(detected="ru")
    asr = ParticipantASR("p-1", runner)  # type: ignore[arg-type]
    asr._transcribe(audio(2.0))

    runner.detected = "en"
    runner.probability = 0.45
    result = asr._transcribe(audio(3.0))

    assert result.language == "ru"
    assert asr.language == "ru"


def test_subtitle_credits_are_not_a_transcript() -> None:
    from app.asr import looks_like_hallucination

    for text in (
        "Субтитры создавал DimaTorzok",
        "Субтитры: DimaTorzok",
        "Редактор субтитров А. Синецкая",
        "Subtitles by Amara.org",
        "Edited by John",
    ):
        assert looks_like_hallucination(text), text

    for text in ("Субтитры нужны для отчёта", "Перевод статьи занял неделю", "Всем привет!"):
        assert not looks_like_hallucination(text), text
