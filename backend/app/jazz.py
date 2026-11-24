"""Real pipeline: browser agent → per-participant ASR → protocol events.

The pipeline owns the browser and the Whisper model; the session owns the event
log. Nothing in here knows about HTTP or the UI.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import protocol as p
from .asr import ASREvent, ParticipantASR, get_runner
from .capture import BrowserAgent
from .faces import make_detector
from .session import Emitter

log = logging.getLogger(__name__)


class JazzPipeline:
    """Joins a meeting in a browser and transcribes each participant's track."""

    name = "jazz"

    def __init__(
        self,
        meeting_url: str,
        *,
        headless: bool = True,
        channel: str | None = "msedge",
        model: str = "large-v3-turbo",
        device: str = "auto",
        language: str = "ru",
        partial_interval_ms: int = 1200,
    ) -> None:
        self.meeting_url = meeting_url
        self.headless = headless
        self.channel = channel
        self.model = model
        self.device = device
        self.language = language
        self.partial_interval_ms = partial_interval_ms

        self.agent = BrowserAgent(meeting_url, headless=headless, channel=channel)
        self.runner = get_runner(model, device=device, language=language)
        self.detector = make_detector()
        self._participants: dict[str, str] = {}  # track_id -> participant_id
        self._asr: dict[str, ParticipantASR] = {}
        self._face_tasks: list[asyncio.Task[None]] = []
        self._emit: Emitter | None = None
        self._index = 0
        self._stats = {"chunks": 0, "bytes": 0, "finals": 0, "partials": 0, "faces": 0}

    # -- lifecycle -----------------------------------------------------------

    async def run(self, emit: Emitter, deadline_ms: int | None) -> None:
        self._emit = emit
        started = p.now_ms()

        def at() -> int:
            return p.now_ms() - started

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
                    f"Не удалось подключиться: {error}. Проверьте ссылку и доступ к встрече.",
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
        try:
            while True:
                if deadline_ms is not None and p.now_ms() - started >= deadline_ms:
                    await emit(p.ev_done(at(), "deadline"))
                    break
                await asyncio.sleep(0.5)
                if reader.done():
                    break
        finally:
            reader.cancel()
            for task in self._face_tasks:
                task.cancel()
            await asyncio.gather(reader, *self._face_tasks, return_exceptions=True)
            await self._flush(at)
            await self.agent.close()
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

    async def _flush(self, at: Any) -> None:
        for track_id, asr in self._asr.items():
            events = await asyncio.to_thread(asr.flush)
            await self._publish(track_id, events, at)

    def set_artifacts_dir(self, path: Any) -> None:
        """Where to drop diagnostic screenshots (used by the API layer)."""
        self._artifacts_dir = path

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
        self._asr[track_id] = ParticipantASR(
            participant_id,
            self.runner,
            partial_interval_ms=self.partial_interval_ms,
        )
        participant = p.new_participant(
            participant_id, name or f"Участник {self._index}", color_index=self._index
        )
        participant["joined_at_ms"] = p.now_ms() - self._clock_start
        await self._emit(p.ev_participant(p.now_ms() - self._clock_start, participant))

        info = self.agent._tracks.get(track_id)
        if info is not None and info.video_track_id:
            self._face_tasks.append(
                asyncio.create_task(self._face_loop(participant_id, info.video_track_id))
            )
        elif self.detector.name == "none":
            await self._emit(
                p.ev_face(p.now_ms() - self._clock_start, participant_id, "none"),
            )

        if self.runner.resolved:
            await self._emit(
                p.ev_notice(
                    p.now_ms() - self._clock_start,
                    f"Распознавание: {self.runner.model_name} ({self.runner.resolved}).",
                )
            )

    async def _face_loop(self, participant_id: str, video_track_id: str) -> None:
        """Grab a few frames per participant and report what was found.

        The frame itself is the fallback: even with no detector installed the UI
        shows the participant's real video instead of an empty avatar, which is
        honest — a frame arrived, no face was identified.
        """
        assert self._emit is not None
        for attempt in range(3):
            await asyncio.sleep(4 if attempt == 0 else 6)
            frame = await self.agent.grab_face(video_track_id)
            if not frame:
                continue
            detection = None
            if self.detector.name != "none":
                try:
                    detection = await asyncio.to_thread(self.detector.detect, frame)
                except Exception:  # noqa: BLE001 - a bad frame must not stop the meeting
                    log.exception("face detection failed")
            at = p.now_ms() - self._clock_start
            if detection is not None:
                self._stats["faces"] += 1
                await self._emit(
                    p.ev_face(at, participant_id, "detected", detection.confidence, detection.thumbnail)
                )
            else:
                await self._emit(p.ev_face(at, participant_id, "frame", None, frame))
            return

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
                        },
                    )
                )
            elif event.kind == "final" and event.text:
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
                        },
                    )
                )


def build_jazz(meeting_url: str, **options: Any) -> JazzPipeline:
    return JazzPipeline(meeting_url, **options)
