"""Real pipeline: browser agent → per-participant ASR → protocol events.

The pipeline owns the browser and the Whisper model; the session owns the event
log. Nothing in here knows about HTTP or the UI.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable, Iterator
from typing import Any

from . import protocol as p
from .asr import ASREvent, ParticipantASR, get_runner, prompt_from_env
from .capture import BrowserAgent
from .errors import describe_error
from .faces import make_detector
from .recording import SessionRecorder
from .session import Emitter

log = logging.getLogger(__name__)

#: Camera watcher: every 10 s for the first two minutes, then every 30 s while
#: the person stays. Frequent early checks catch a camera switched on late;
#: rare later ones let a better frame replace a poor one without spamming events.
FACE_FAST_INTERVAL_S = 10.0
FACE_FAST_ATTEMPTS = 12
FACE_SLOW_INTERVAL_S = 30.0
#: Announce "без камеры" only after this long with no frame at all.
FACE_ABSENCE_AFTER_S = 240.0
#: A new picture must be this much better to replace the current one.
FACE_CONFIDENCE_STEP = 0.08



def recording_enabled() -> bool:
    """Recording is on by default; ``TRANSCRIBE_RECORD=0`` turns it off."""
    return os.environ.get("TRANSCRIBE_RECORD", "1").strip().lower() not in {"0", "false", "no"}


def empty_room_timeout() -> int:
    """Seconds to wait after the last person leaves before finishing (0 = never).

    A meeting where everyone has gone is a meeting that produced its transcript
    already; leaving a browser parked there costs memory and confuses the UI.
    """
    try:
        return max(0, int(os.environ.get("TRANSCRIBE_EMPTY_ROOM_TIMEOUT", "60")))
    except ValueError:
        return 60


class JazzPipeline:
    """Joins a meeting in a browser and transcribes each participant's track."""

    name = "jazz"

    def __init__(
        self,
        meeting_url: str,
        *,
        headless: bool = True,
        channel: str | None = "chrome",
        model: str = "large-v3-turbo",
        device: str = "auto",
        language: str | None = None,
        partial_interval_ms: int = 1200,
        agent: BrowserAgent | None = None,
        password: str | None = None,
        empty_room_timeout_s: int | None = None,
    ) -> None:
        self.meeting_url = meeting_url
        self.headless = headless
        self.channel = channel
        self.model = model
        self.device = device
        self.language = language
        self.partial_interval_ms = partial_interval_ms
        self.password = password
        self.empty_room_timeout_s = (
            empty_room_timeout_s if empty_room_timeout_s is not None else empty_room_timeout()
        )

        # Injectable so the failure paths can be tested without a browser.
        self.agent = agent or BrowserAgent(
            meeting_url, headless=headless, channel=channel, password=password
        )
        self.runner = get_runner(model, device=device, language=language, prompt=prompt_from_env())
        self.detector = make_detector()
        self._participants: dict[str, str] = {}  # track_id -> participant_id
        self._participant_state: dict[str, dict[str, Any]] = {}  # track_id -> session dict
        self._asr: dict[str, ParticipantASR] = {}
        self._face_tasks: list[asyncio.Task[None]] = []
        self._emit: Emitter | None = None
        self._index = 0
        self.recorder: SessionRecorder | None = None
        self._stats = {"chunks": 0, "bytes": 0, "finals": 0, "partials": 0, "faces": 0}
        #: Last final per track, to drop a phrase the decoder emits twice.
        self._last_final: dict[str, tuple[str, int]] = {}
        self._saw_human = False
        self._empty_since: float | None = None

    # -- lifecycle -----------------------------------------------------------

    async def run(self, emit: Emitter, deadline_ms: int | None) -> None:
        self._emit = emit
        started = p.now_ms()

        def at() -> int:
            return p.now_ms() - started

        self._start_recording()

        await emit(p.ev_notice(at(), "Запускаю браузер и подключаюсь к встрече…"))
        await emit(
            p.ev_participant(
                at(),
                p.new_participant("p-agent", "Ассистент (бот)", role="bot", has_video=False),
            )
        )

        try:
            await self.agent.start()
        except Exception as error:  # noqa: BLE001 - surfaced to the UI
            await self._screenshot(at(), "join-failed")
            await emit(
                p.ev_notice(
                    at(),
                    f"Не удалось подключиться: {describe_error(error)}",
                    "warn",
                )
            )
            raise

        for note in self.agent.notes[-6:]:
            await emit(p.ev_notice(at(), f"Вход: {note}"))

        if not self.agent._tracks:
            shot = await self._screenshot(at(), "no-tracks")
            await emit(
                p.ev_notice(
                    at(),
                    "Агент вошёл, но медиапотоков нет: возможно, встреча ждёт в лобби, "
                    "требует входа по SSO или никто ещё не включил микрофон."
                    + (f" Скриншот страницы: {shot}" if shot else ""),
                    "warn",
                )
            )

        self._clock_start = started
        reader = asyncio.create_task(self._read_audio())
        watcher = asyncio.create_task(self._participant_loop())
        lost_browser = False
        try:
            while True:
                if deadline_ms is not None and p.now_ms() - started >= deadline_ms:
                    await emit(p.ev_done(at(), "deadline"))
                    break
                await asyncio.sleep(0.5)
                if not self.agent.is_alive():
                    # Hanging here would look like a frozen demo: the browser is
                    # gone and no audio will ever arrive again.
                    lost_browser = True
                    await emit(
                        p.ev_notice(
                            at(),
                            f"Браузер агента закрылся ({self.agent.crash_reason or 'страница недоступна'}) — "
                            "транскрибация прервана.",
                            "warn",
                        )
                    )
                    break
                if await self._room_is_empty(at()):
                    break
                if reader.done():
                    break
        finally:
            reader.cancel()
            watcher.cancel()
            for task in self._face_tasks:
                task.cancel()
            await asyncio.gather(reader, watcher, *self._face_tasks, return_exceptions=True)
            await self._flush(at)
            await self.agent.close()
            tracks, recorded_bytes = self._stop_recording()
            if tracks:
                await emit(
                    p.ev_notice(
                        at(),
                        f"Записано аудио: {tracks} дорожек, {recorded_bytes / 1_000_000:.1f} МБ.",
                    )
                )
            await emit(
                p.ev_notice(
                    at(),
                    "Готово: чанков {chunks}, речи {seconds:.0f} с, реплик {finals}.".format(
                        chunks=self._stats["chunks"],
                        seconds=self._stats["bytes"] / 32000,
                        finals=self._stats["finals"],
                    ),
                )
            )

        if lost_browser:
            raise RuntimeError("браузер агента закрылся или упал — сессия прервана")

    async def _flush(self, at: Any) -> None:
        for track_id, asr in self._asr.items():
            events = await asyncio.to_thread(asr.flush)
            await self._publish(track_id, events, at)

    async def _room_is_empty(self, at_ms: int) -> bool:
        """Finish the meeting once everybody has left and stayed away.

        The track stays alive at the SFU after a person hangs up, so the room's
        participant list is what tells us they are gone. A grace period keeps a
        reconnect from ending the session.
        """
        if not self.empty_room_timeout_s or not self._participant_state:
            return False
        if getattr(self.agent, "human_present", True):
            self._saw_human = True
            self._empty_since = None
            return False
        if not self._saw_human:
            return False
        loop = asyncio.get_running_loop()
        if self._empty_since is None:
            self._empty_since = loop.time()
            return False
        if loop.time() - self._empty_since < self.empty_room_timeout_s:
            return False

        assert self._emit is not None
        await self._emit(
            p.ev_notice(
                at_ms,
                "В комнате не осталось участников — запись и распознавание завершены.",
            )
        )
        return True

    async def _participant_loop(self) -> None:
        """Apply name changes and departures reported by the browser agent."""
        updates = getattr(self.agent, "updates", None)
        if updates is None:
            return
        while True:
            update = await updates.get()
            try:
                await self._apply_participant_update(update)
            except Exception:  # noqa: BLE001 - one bad update must not stop the meeting
                log.exception("participant update failed")

    async def _apply_participant_update(self, update: Any) -> None:
        """Send only what changed; the session merges it into the participant.

        Sending the whole snapshot here reset fields the pipeline does not track,
        such as the face status and the thumbnail, so a rename erased a detected
        face. The event is a patch by design.
        """
        assert self._emit is not None
        state = self._participant_state.get(update.track_id)
        if state is None:
            return

        patch: dict[str, Any] = {"id": state["id"]}
        name = getattr(update, "name", None)
        if name and name != state.get("display_name"):
            log.info("participant %s renamed to %s", state["id"], name)
            state["display_name"] = name
            patch["display_name"] = name
        present = getattr(update, "present", None)
        if present is not None and present != state.get("present", True):
            state["present"] = present
            if not present:
                state["speaking"] = False
            patch["present"] = present
            if not present:
                patch["speaking"] = False
        if len(patch) > 1:
            await self._emit(p.ev_participant(p.now_ms() - self._clock_start, patch))

    def set_artifacts_dir(self, path: Any) -> None:
        """Where to drop diagnostic screenshots and recorded audio (set by the API)."""
        self._artifacts_dir = path

    def _start_recording(self) -> None:
        directory = getattr(self, "_artifacts_dir", None)
        if directory is None or not recording_enabled():
            if not recording_enabled():
                log.info("recording: disabled by TRANSCRIBE_RECORD")
            return
        try:
            self.recorder = SessionRecorder(directory / "audio")
            log.info("recording: writing to %s", directory / "audio")
        except Exception:  # noqa: BLE001 - a full disk must not stop the meeting
            log.exception("recording: cannot start")
            self.recorder = None

    def _stop_recording(self) -> tuple[int, int]:
        """Close the files and report how many tracks and bytes were written."""
        if self.recorder is None:
            return 0, 0
        try:
            manifest = self.recorder.manifest()
            total = self.recorder.total_bytes
        except Exception:  # noqa: BLE001
            manifest, total = [], 0
        self.recorder.close()
        self.recorder = None
        return len(manifest), total

    async def _screenshot(self, at: Any, reason: str) -> str | None:
        """Save what the page looked like — the fastest way to explain a failed join."""
        directory = getattr(self, "_artifacts_dir", None)
        if directory is None:
            return None
        try:
            target = directory / f"agent-{reason}.png"
            await self.agent.screenshot(str(target))
            log.info("agent screenshot: %s", target)
            return target.name
        except Exception:  # noqa: BLE001 - diagnostics must never break the session
            log.debug("screenshot failed", exc_info=True)
            return None
    # -- audio path ----------------------------------------------------------

    async def _read_audio(self) -> None:
        async for chunk in self.agent.chunks():
            self._stats["chunks"] += 1
            self._stats["bytes"] += len(chunk.pcm)

            if chunk.track_id not in self._asr:
                await self._register_track(chunk.track_id, chunk.participant_name)

            asr = self._asr[chunk.track_id]
            if self.recorder is not None:
                self.recorder.add(self._participants[chunk.track_id], chunk.pcm)
            try:
                events = await asyncio.to_thread(asr.push, chunk.pcm)
            except Exception:  # noqa: BLE001 - one bad chunk must not kill the meeting
                log.exception("asr failed for track %s", chunk.track_id[:8])
                continue
            await self._publish(chunk.track_id, events, lambda: p.now_ms() - self._clock_start)

    async def _register_track(self, track_id: str, name: str) -> None:
        assert self._emit is not None
        self._index += 1
        participant_id = f"p-{self._index}"
        self._participants[track_id] = participant_id
        # The agent may have learned the real name after the audio started.
        info = self.agent._tracks.get(track_id)
        resolved = (info.name if info and info.name else "") or name
        self._asr[track_id] = ParticipantASR(
            participant_id,
            self.runner,
            partial_interval_ms=self.partial_interval_ms,
        )
        participant = p.new_participant(
            participant_id, resolved or f"Участник {self._index}", color_index=self._index
        )
        participant["joined_at_ms"] = p.now_ms() - self._clock_start
        self._participant_state[track_id] = participant
        await self._emit(p.ev_participant(p.now_ms() - self._clock_start, participant))

        # Always watch for a camera, whatever we saw at join time: people switch
        # one on mid-meeting, and "no video track yet" is not "no camera".
        self._face_tasks.append(asyncio.create_task(self._face_loop(participant_id, track_id)))

        if self.runner.resolved:
            await self._emit(
                p.ev_notice(
                    p.now_ms() - self._clock_start,
                    f"Распознавание: {self.runner.model_name} ({self.runner.resolved}).",
                )
            )

    def _face_schedule(self) -> Iterator[float]:
        """Frequent at first, then rare: a face is allowed to improve over time."""
        for _ in range(FACE_FAST_ATTEMPTS):
            yield FACE_FAST_INTERVAL_S
        while True:
            yield FACE_SLOW_INTERVAL_S

    async def _face_loop(
        self,
        participant_id: str,
        track_id: str,
        *,
        delays: Iterable[float] | None = None,
        absence_after_s: float = FACE_ABSENCE_AFTER_S,
    ) -> None:
        """Watch a participant's camera and keep the best frame.

        The first useful frame used to win forever, so a single bad moment — a
        turned head, a 40% detection, a blurry frame — stayed in the tile for the
        whole meeting. The loop now runs for as long as the participant is here
        and replaces the picture only when the new one is better:

        * a detected face replaces nothing or a weaker face (by a clear margin);
        * a plain frame is shown once, only while no face is known yet;
        * "без камеры" is announced only after the camera gave us nothing at all.

        The frame itself is also the honest fallback: with no detector installed
        the UI shows the real video instead of an empty avatar.
        """
        assert self._emit is not None
        loop = asyncio.get_running_loop()
        started = loop.time()
        best_confidence = 0.0
        has_face = False
        has_frame = False
        announced_absence = False

        for delay in delays if delays is not None else self._face_schedule():
            await asyncio.sleep(delay)
            frame = await self.agent.grab_face(track_id)

            if not frame:
                if (
                    not has_frame
                    and not announced_absence
                    and loop.time() - started >= absence_after_s
                ):
                    announced_absence = True
                    await self._emit(p.ev_face(p.now_ms() - self._clock_start, participant_id, "none"))
                continue

            detection = None
            if self.detector.name != "none":
                try:
                    detection = await asyncio.to_thread(self.detector.detect, frame)
                except Exception:  # noqa: BLE001 - a bad frame must not stop the meeting
                    log.exception("face detection failed")

            if detection is None:
                if not has_frame and not has_face:
                    has_frame = True
                    await self._emit(p.ev_face(p.now_ms() - self._clock_start, participant_id, "frame", None, frame))
                continue

            improved = detection.confidence >= best_confidence + FACE_CONFIDENCE_STEP
            if has_face and not improved:
                continue
            if improved or not has_face:
                has_face = True
                best_confidence = max(best_confidence, detection.confidence)
                self._stats["faces"] += 1
                await self._emit(
                    p.ev_face(
                        p.now_ms() - self._clock_start,
                        participant_id,
                        "detected",
                        detection.confidence,
                        detection.thumbnail,
                    )
                )


    async def _publish(self, track_id: str, events: list[ASREvent], at: Any) -> None:
        assert self._emit is not None
        participant_id = self._participants.get(track_id)
        if participant_id is None:
            return
        for event in events:
            if event.kind == "partial":
                self._stats["partials"] += 1
                await self._emit(
                    p.ev_segment(
                        at(),
                        {
                            "id": f"{participant_id}-live",
                            "participant_id": participant_id,
                            "start_ms": event.start_ms,
                            "end_ms": event.end_ms,
                            "text": event.text,
                            "final": False,
                            "confidence": None,
                            "language": event.language,
                        },
                    )
                )
            elif event.kind == "final" and event.text:
                normalized = " ".join(event.text.lower().split())
                previous = self._last_final.get(track_id)
                if previous and previous[0] == normalized and abs(event.start_ms - previous[1]) <= 2000:
                    # The decoder re-emitted the same phrase for the same words;
                    # keeping both would double every "алло" in the protocol.
                    log.debug("dropping duplicate final for %s: %s", track_id[:8], event.text)
                    continue
                self._last_final[track_id] = (normalized, event.start_ms)
                self._stats["finals"] += 1
                await self._emit(
                    p.ev_segment(
                        at(),
                        {
                            "id": f"{participant_id}-{event.start_ms}",
                            "participant_id": participant_id,
                            "start_ms": event.start_ms,
                            "end_ms": event.end_ms,
                            "text": event.text,
                            "final": True,
                            "confidence": None,
                            "language": event.language,
                        },
                    )
                )


def build_jazz(meeting_url: str, **options: Any) -> JazzPipeline:
    return JazzPipeline(meeting_url, **options)
