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
import re
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
    #: Language the utterance was spoken in, as detected by Whisper.
    language: str | None = None


#: Whisper was trained on subtitled video, so on clicks and music it "remembers"
#: closing credits. These phrases never belong in a meeting transcript: without
#: this filter a silent microphone with a keyboard produced "Продолжение
#: следует…", "Смотрите продолжение в следующей серии" and
#: "ご視聴ありがとうございました".
HALLUCINATION_PHRASES = (
    "продолжение следует",
    "продолжение в следующей серии",
    "смотрите продолжение",
    "спасибо за просмотр",
    "подписывайтесь на канал",
    "ставьте лайки",
    "нажмите на колокольчик",
    "звучит музыка",
    "играет музыка",
    "субтитры сделал",
    "субтитры подготовил",
    "субтитры создавал",
    "субтитры создал",
    "субтитры созданы",
    "субтитры от",
    "dimatorzok",
    "синецкая",
    "редактор субтитров",
    "корректор субтитров",
    "thanks for watching",
    "please subscribe",
    "subscribe to my channel",
    "like and subscribe",
    "ご視聴ありがとうございました",
    "最後までご視聴",
    "チャンネル登録",
)

#: The list above cannot cover every wording, so the shapes that give credits away
#: are matched too: "Субтитры создавал <никто>", "Субтитры: …", "Subtitles by …".
HALLUCINATION_PATTERNS = (
    # "Субтитры создавал DimaTorzok", "Субтитры: DimaTorzok", "Титры — …".
    # A sentence that merely starts with the word ("Субтитры нужны для отчёта")
    # must survive, so the verb or the colon is required.
    re.compile(r"^(субтитры|титры)\s*(?:[:—–-]|\s+(?:созда|сдела|подготов|редакт|перев|выполн))"),
    re.compile(r"^(редактор|корректор)\s+субтитр"),
    re.compile(r"^(перевод|озвучка|озвучивание)\s*[:—–-]\s*\S"),
    re.compile(r"^(subtitles?|captions?)\s*(?:[:—–-]|\s+(?:by|created|made|provided))"),
    re.compile(r"^(edited|translated|proofread|subtitled)\s+by\b"),
    re.compile(r"\b(dimatorzok|синецкая)\b"),
)

#: Words from the glossary. On silence or noise Whisper "continues" the prompt and
#: returns these instead of speech ("Jazz, Jazz"), so an answer made only of them
#: is not a transcript either.
_TOKEN_RE = re.compile(r"[0-9a-zа-я]+")


def prompt_tokens(prompt: str | None) -> frozenset[str]:
    if not prompt:
        return frozenset()
    return frozenset(_TOKEN_RE.findall(normalize_text(prompt)))


def looks_like_prompt_echo(text: str, tokens: frozenset[str]) -> bool:
    """True when the "transcript" is just words borrowed from the prompt."""
    words = _TOKEN_RE.findall(normalize_text(text))
    if not words or len(words) > 4 or not tokens:
        return False
    return all(word in tokens for word in words)


#: Segment statistics below these values mean "the model was guessing".
NO_SPEECH_MAX = 0.6
AVG_LOGPROB_MIN = -1.0
COMPRESSION_MAX = 2.4


def normalize_text(text: str) -> str:
    """Lowercase, collapse spaces, drop punctuation — for phrase matching."""
    lowered = " ".join(text.lower().replace("ё", "е").split())
    return lowered.strip(" .,!?…—-–\"'«»()[]{}:;")


def looks_like_hallucination(text: str) -> bool:
    """True for stock subtitle credits and for text with no words at all."""
    normalized = normalize_text(text)
    if not normalized:
        return True
    if not any(character.isalnum() for character in normalized):
        return True
    if any(phrase in normalized for phrase in HALLUCINATION_PHRASES):
        return True
    return any(pattern.search(normalized) for pattern in HALLUCINATION_PATTERNS)


def segment_is_noise(segment: Any) -> bool:
    """Trust Whisper's own confidence: a guess is not a transcript."""
    if getattr(segment, "no_speech_prob", 0.0) > NO_SPEECH_MAX:
        return True
    if getattr(segment, "avg_logprob", 0.0) < AVG_LOGPROB_MIN:
        return True
    return getattr(segment, "compression_ratio", 0.0) > COMPRESSION_MAX


#: Vocabulary that Whisper otherwise guesses: the product name came back as
#: "Джесс" without it. A mixed RU/EN glossary keeps both languages intact.
DEFAULT_PROMPT = (
    "SaluteJazz, Jazz, транскрибация, transcription, планёрка, deadline, спикер, реплики."
)


def prompt_from_env(default: str | None = DEFAULT_PROMPT) -> str | None:
    """``TRANSCRIBE_PROMPT`` overrides the glossary; an empty value disables it."""
    value = os.environ.get("TRANSCRIBE_PROMPT")
    if value is None:
        return default
    return value.strip() or None


@dataclass(slots=True)
class Transcript:
    """One transcription call: the text plus the language it was spoken in."""

    text: str = ""
    language: str | None = None
    probability: float | None = None


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
        min_voiced_run_ms: int = 150,
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
        #: Keyboard clicks are single loud frames. Speech is a sustained run, so a
        #: segment also needs one uninterrupted voiced stretch of this length.
        self.min_voiced_run_frames = max(1, int(min_voiced_run_ms / frame_ms))
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
        self._run = 0
        self._max_run = 0
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
                    self._run = 1
                    self._max_run = 1
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
                    self._run += 1
                    self._max_run = max(self._max_run, self._run)
                    self._trailing = 0
                    self._silence_run = 0
                else:
                    self._run = 0
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
        max_run = self._max_run
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
        self._run = 0
        self._max_run = 0
        self._trailing = 0
        self._silence_run = 0
        self._preroll.clear()

        events = [DetectorEvent("speech_stop", start_ms=self._ms(end_index), end_ms=self._ms(end_index))]
        if voiced < self.min_voiced_frames:
            return events
        if max_run < self.min_voiced_run_frames:
            # Short bursts: typing, a door, a notification. Sending this to
            # Whisper is how "Продолжение следует…" ends up in the transcript.
            log.debug("detector: dropped %d ms of impulsive noise", voiced * self.frame_ms)
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
        language: str | None = None,
        beam_size: int = 5,
        prompt: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.prompt = prompt
        self._prompt_tokens = prompt_tokens(prompt)
        self.resolved: str | None = None
        self._model: Any = None
        self._lock = threading.Lock()  # guards lazy model construction
        self._infer_lock = threading.Lock()  # ctranslate2 is not thread-safe

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

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        greedy: bool = False,
        language: str | None = None,
    ) -> Transcript:
        """Transcribe one utterance.

        ctranslate2 models are not thread-safe, and several participants are
        transcribed from different worker threads. Finals therefore wait for the
        lock; interim revisions give up immediately instead of queueing up behind
        the final, which would turn a live transcript into a backlog.

        ``language=None`` lets Whisper detect the language per utterance instead
        of forcing one: with a forced language, English speech comes back as
        Russian words, which destroys the meaning of a mixed-language meeting.
        """
        if audio.size == 0:
            return Transcript()

        if greedy:
            if not self._infer_lock.acquire(blocking=False):
                return Transcript()
            try:
                return self._run(audio, beam_size=1, language=language)
            finally:
                self._infer_lock.release()

        with self._infer_lock:
            return self._run(audio, beam_size=self.beam_size, language=language)

    def _run(self, audio: np.ndarray, *, beam_size: int, language: str | None) -> Transcript:
        model = self.ensure()
        # A configured language wins: the operator asked for it explicitly.
        target = self.language or language
        segments, info = model.transcribe(
            audio,
            language=target,
            beam_size=beam_size,
            vad_filter=False,  # VAD already ran upstream
            condition_on_previous_text=False,
            without_timestamps=True,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            initial_prompt=self.prompt,
        )
        kept: list[str] = []
        for segment in segments:
            if segment_is_noise(segment):
                log.debug("asr: dropped a low-confidence segment")
                continue
            candidate = segment.text.strip()
            if looks_like_hallucination(candidate):
                log.debug("asr: dropped a stock phrase: %s", candidate[:60])
                continue
            if looks_like_prompt_echo(candidate, self._prompt_tokens):
                log.debug("asr: dropped prompt echo: %s", candidate[:60])
                continue
            kept.append(candidate)

        text = " ".join(kept).strip()
        return Transcript(
            text=text,
            language=getattr(info, "language", None),
            probability=getattr(info, "language_probability", None),
        )


_RUNNERS: dict[tuple[str, str, str], WhisperRunner] = {}
_RUNNERS_LOCK = threading.Lock()


def get_runner(
    model_name: str = "large-v3-turbo",
    *,
    device: str = "auto",
    language: str | None = None,
    prompt: str | None = None,
) -> WhisperRunner:
    """One runner per (model, device, language), shared by every session.

    Loading 1.6 GB of weights per meeting would make the second session in a row
    feel broken; the model is read-only after construction, so sharing is safe.
    """
    key = (model_name, device, language, prompt or "")
    with _RUNNERS_LOCK:
        runner = _RUNNERS.get(key)
        if runner is None:
            runner = WhisperRunner(model_name, device=device, language=language, prompt=prompt)
            _RUNNERS[key] = runner
        return runner


#: Short audio is not evidence of a language: below these limits Whisper's own
#: detection is unreliable, and "Всем привет!" used to be tagged as English.
LANGUAGE_DETECT_MIN_S = 1.5
LANGUAGE_SWITCH_MIN_S = 2.5
LANGUAGE_SWITCH_PROBABILITY = 0.7


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
        #: Last accepted language. A one-word "да" is too short to detect
        #: reliably, so short utterances reuse this instead of letting Whisper
        #: guess again and flip the language mid-sentence.
        self.language: str | None = None
        self.detect_min_samples = int(SAMPLE_RATE * LANGUAGE_DETECT_MIN_S)

    def _language_for(self, audio: np.ndarray) -> str | None:
        """Which language to ask for: pinned, auto-detected, or last known."""
        if self.runner.language:
            return self.runner.language
        if self.language is None:
            # Nothing to stick to yet: let Whisper decide, whatever the length.
            return None
        if audio.size >= int(SAMPLE_RATE * LANGUAGE_SWITCH_MIN_S):
            # Long enough for detection to be trustworthy; this is how a meeting
            # that switches to English is picked up.
            return None
        return self.language

    def _transcribe(self, audio: np.ndarray, *, greedy: bool = False) -> Transcript:
        result = self.runner.transcribe(audio, greedy=greedy, language=self._language_for(audio))
        if self.runner.language or not result.language:
            return result  # pinned by the operator, or nothing detected

        seconds = audio.size / SAMPLE_RATE
        probability = result.probability or 0.0
        if self.language is None:
            if seconds >= LANGUAGE_DETECT_MIN_S:
                self.language = result.language
            return result

        if (
            result.language != self.language
            and seconds >= LANGUAGE_SWITCH_MIN_S
            and probability >= LANGUAGE_SWITCH_PROBABILITY
        ):
            # A long, confident utterance in another language: accept the switch.
            log.info("asr: language switched %s -> %s", self.language, result.language)
            self.language = result.language
            return result

        if result.language != self.language:
            # A short phrase is not evidence. Report the language the text is
            # actually in, so "Всем привет!" is not tagged as English.
            return Transcript(text=result.text, language=self.language, probability=result.probability)
        return result

    def push(self, pcm: bytes) -> list[ASREvent]:
        audio = pcm16_to_float(pcm)
        if audio.size == 0:
            return []

        out: list[ASREvent] = []
        for event in self.detector.push(audio):
            if event.kind == "segment" and event.pcm is not None:
                result = self._transcribe(event.pcm)
                if result.text:
                    out.append(
                        ASREvent(
                            "final",
                            text=result.text,
                            start_ms=event.start_ms,
                            end_ms=event.end_ms,
                            language=result.language,
                        )
                    )
                    self._last_partial = ""

        self._stream_ms += int(audio.size * 1000 / SAMPLE_RATE)
        if self.detector.in_speech and self._stream_ms >= self._next_partial_ms:
            buffer = self.detector.buffered_audio()
            if buffer.size >= SAMPLE_RATE // 2:
                result = self._transcribe(buffer, greedy=True)
                if result.text and result.text != self._last_partial:
                    self._last_partial = result.text
                    out.append(
                        ASREvent(
                            "partial",
                            text=result.text,
                            start_ms=self.detector.buffer_start_ms,
                            end_ms=self._stream_ms,
                            language=result.language,
                        )
                    )
                self._next_partial_ms = self._stream_ms + self.partial_interval_ms
        return out

    def flush(self) -> list[ASREvent]:
        out: list[ASREvent] = []
        for event in self.detector.flush():
            if event.kind == "segment" and event.pcm is not None:
                result = self._transcribe(event.pcm)
                if result.text:
                    out.append(
                        ASREvent(
                            "final",
                            text=result.text,
                            start_ms=event.start_ms,
                            end_ms=event.end_ms,
                            language=result.language,
                        )
                    )
        return out
