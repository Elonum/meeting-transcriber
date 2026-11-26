"""Real pipeline: browser agent → per-participant ASR → protocol events.

The pipeline owns the browser and the Whisper model; the session owns the event
log. Nothing in here knows about HTTP or the UI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Iterable, Iterator
from typing import Any

from . import protocol as p
from .asr import ASREvent, ParticipantASR, get_runner, prompt_from_env
from .capture import UNNAMED_PREFIX, BrowserAgent
from .errors import describe_error
from .faces import make_detector
from .identity import (
    MIN_FACE_CONFIDENCE,
    MIN_IDENTITY_FACE_PX,
    FaceMatch,
    IdentityBook,
    identity_enabled,
)
from .identity import settings as identity_settings
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
        threshold, margin, confirmations = identity_settings()
        self.identity: IdentityBook | None = (
            IdentityBook(threshold=threshold, margin=margin, confirmations=confirmations)
            if identity_enabled() and getattr(self.detector, "embedder", None) is not None
            else None
        )
        #: participant id -> record, so a face match can find the record fast.
        self._by_id: dict[str, dict[str, Any]] = {}
        self._participants: dict[str, str] = {}  # track_id -> participant_id
        self._participant_state: dict[str, dict[str, Any]] = {}  # track_id -> session dict
        #: Participant records by display name, so somebody who leaves and comes
        #: back under the same name keeps one transcript instead of a new tile.
        self._by_name: dict[str, dict[str, Any]] = {}
        #: participant_id -> the track that currently carries them.
        self._live: dict[str, str] = {}
        #: Session-clock value of each track's first sample, for the timecodes.
        self._track_start_ms: dict[str, int] = {}
        #: Participants who already produced a line; their record cannot be
        #: re-pointed to another id without losing the line.
        self._spoken: set[str] = set()
        #: Placeholder records to withdraw once their replacement is emitted.
        self._pending_removal: list[str] = []
        self._asr: dict[str, ParticipantASR] = {}
        self._face_tasks: list[asyncio.Task[None]] = []
        #: Watcher per participant, so it can be stopped when that person leaves
        #: instead of polling a dead track until the meeting ends.
        self._face_by_participant: dict[str, asyncio.Task[None]] = {}
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
        self._clock_start = started

        def at() -> int:
            # One clock for every event: notices and segments must sort together.
            return p.now_ms() - self._clock_start

        self._start_recording()

        # Emitted at 0: the clock is restarted when the agent actually enters the
        # room, so these two must not carry the browser-startup delay.
        await emit(p.ev_notice(0, "Запускаю браузер и подключаюсь к встрече…"))
        await emit(
            p.ev_participant(
                0,
                p.new_participant("p-agent", "Ассистент", role="bot", has_video=False),
            )
        )

        try:
            await self.agent.start()
        except Exception as error:  # noqa: BLE001 - surfaced to the UI
            await self._screenshot("join-failed")
            await emit(
                p.ev_notice(
                    at(),
                    f"Не удалось подключиться: {describe_error(error)}",
                    "warn",
                )
            )
            raise

        # The transcript starts at 00:00 when the agent enters the room: browser
        # startup (30-40 s) used to shift every timecode away from the meeting.
        self._clock_start = p.now_ms()

        for note in self.agent.notes[-6:]:
            await emit(p.ev_notice(at(), f"Вход: {note}"))

        if self.identity is not None:
            await emit(
                p.ev_notice(
                    at(),
                    "Идентификация по лицу включена: шаблоны сравниваются только внутри "
                    "этой встречи и не сохраняются.",
                )
            )

        if not self.agent._tracks:
            shot = await self._screenshot("no-tracks")
            await emit(
                p.ev_notice(
                    at(),
                    "Агент вошёл, но медиапотоков нет: возможно, встреча ждёт в лобби, "
                    "требует входа по SSO или никто ещё не включил микрофон."
                    + (f" Скриншот страницы: {shot}" if shot else ""),
                    "warn",
                )
            )

        # One clock for the whole session, set once when it starts: notices,
        # participants and segments must all sort on the same timeline. Segment
        # times are speech times, so a slow transcript still lands in order.
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
            await self._flush()
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

    async def _flush(self) -> None:
        for track_id, asr in self._asr.items():
            events = await asyncio.to_thread(asr.flush)
            await self._publish(track_id, events)

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
            adopted = self._adopt_if_returning(update.track_id, state, name)
            if adopted is not None:
                state = adopted
                patch = {"id": state["id"]}
                for gone_id in self._pending_removal:
                    await self._emit(
                        p.ev_participant_removed(p.now_ms() - self._clock_start, gone_id)
                    )
                self._pending_removal.clear()
                await self._emit(p.ev_participant(p.now_ms() - self._clock_start, state))
            resolved = self._rename(state, name)
            if self.identity is not None:
                self.identity.rename(state["id"], resolved)
            log.info("participant %s renamed to %s", state["id"], resolved)
            patch["display_name"] = resolved
        present = getattr(update, "present", None)
        if present is not None and present != state.get("present", True):
            state["present"] = present
            if not present:
                state["speaking"] = False
                # The name is free again: a return under it is the same person,
                # a new name is somebody else, and both are handled by _claim.
                self._live.pop(state["id"], None)
                if self.identity is not None:
                    self.identity.mark_absent(state["id"])
                # Nobody is on screen any more: stop asking for their camera.
                watcher = self._face_by_participant.pop(state["id"], None)
                if watcher is not None and not watcher.done():
                    watcher.cancel()
            patch["present"] = present
            if not present:
                patch["speaking"] = False
        if len(patch) > 1:
            await self._emit(p.ev_participant(p.now_ms() - self._clock_start, patch))

        alert = getattr(update, "alert", None)
        if alert:
            # Silence on one track must be visible: otherwise a broken monitor
            # looks like a participant who simply stopped talking.
            who = state.get("display_name") or state["id"]
            log.warning("participant %s: %s", state["id"], alert)
            await self._emit(
                p.ev_notice(
                    p.now_ms() - self._clock_start,
                    f"{who}: {alert}. Остальные участники записываются.",
                    "warn",
                )
            )

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

    async def _screenshot(self, reason: str) -> str | None:
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
            await self._publish(chunk.track_id, events)

    def _claim_participant(self, name: str) -> dict[str, Any]:
        """Find the participant for a display name, creating one when needed.

        The room gives us names and nothing else, so the name *is* the identity:
        leaving and returning under the same name continues one transcript,
        while a new name is a new participant. Two people who choose one name
        cannot be told apart, so the second is suffixed instead of being merged.
        """
        clean = (name or "").strip()
        if not clean:
            clean = self._next_placeholder()
        existing = self._by_name.get(clean)
        if existing is not None and existing["id"] not in self._live:
            return existing
        if existing is not None:
            clean = self._unique_name(clean)
        self._index += 1
        participant = p.new_participant(f"p-{self._index}", clean, color_index=self._index)
        self._by_name[clean] = participant
        self._by_id[participant["id"]] = participant
        return participant

    def _unique_name(self, base: str) -> str:
        """Second person with the same display name gets an honest suffix."""
        for suffix in range(2, 100):
            candidate = f"{base} ({suffix})"
            if candidate not in self._by_name:
                return candidate
        return f"{base} ({self._index + 1})"

    def _next_placeholder(self) -> str:
        """Placeholder numbers never collide, even after people come and go."""
        numbers = [
            int(match.group(1))
            for name in self._by_name
            for match in [re.search(r"(\d+)$", name)]
            if match
        ]
        return f"{UNNAMED_PREFIX}{max(numbers, default=0) + 1}"

    def _adopt_if_returning(
        self, track_id: str, state: dict[str, Any], name: str
    ) -> dict[str, Any] | None:
        """Let a returning person take back the record they left behind.

        A track is sometimes registered before the room paints its tile, so the
        participant starts out as "Участник N" and only later learns the real
        name. If that name belongs to somebody who has already left and the
        placeholder never spoke, the placeholder simply makes way: otherwise the
        archive would show two tiles for one person.
        """
        clean = (name or "").strip()
        target = self._by_name.get(clean)
        if target is None or target["id"] == state["id"]:
            return None
        if target["id"] in self._live or state["id"] in self._spoken:
            return None

        old_id = state["id"]
        self._by_name.pop(str(state.get("display_name") or ""), None)
        self._live.pop(old_id, None)
        self._participants[track_id] = target["id"]
        self._live[target["id"]] = track_id
        target["present"] = True
        self._participant_state[track_id] = target
        asr = self._asr.get(track_id)
        if asr is not None:
            asr.participant_id = target["id"]

        watcher = self._face_by_participant.pop(old_id, None)
        if watcher is not None and not watcher.done():
            watcher.cancel()
        task = asyncio.create_task(self._face_loop(target["id"], track_id))
        self._face_tasks.append(task)
        self._face_by_participant[target["id"]] = task
        # The empty placeholder must leave the archive before the real record
        # is emitted, otherwise the meeting ends with a spare "Участник N".
        self._pending_removal.append(old_id)
        log.info("participant %s (%s) is back on track %s", target["id"], clean, track_id[:8])
        return target

    def _rename(self, participant: dict[str, Any], wanted: str) -> str:
        """A person changed their display name without reconnecting."""
        clean = (wanted or "").strip()
        current = str(participant.get("display_name") or "")
        if not clean or clean == current:
            return current
        owner = self._by_name.get(clean)
        if owner is not None and owner is not participant:
            # The name belongs to somebody else's record; never reuse it here,
            # or the archive would show two people called the same.
            clean = self._unique_name(clean)
        if self._by_name.get(current) is participant:
            del self._by_name[current]
        participant["display_name"] = clean
        self._by_name[clean] = participant
        return clean

    async def _register_track(self, track_id: str, name: str) -> None:
        assert self._emit is not None
        # The agent may have learned the real name after the audio started.
        info = self.agent._tracks.get(track_id)
        resolved = (info.name if info and info.name else "") or name
        participant = self._claim_participant(resolved)
        participant_id = participant["id"]
        self._participants[track_id] = participant_id
        self._live[participant_id] = track_id
        # The track's own clock starts at its first sample; segments are shifted
        # by this, so a late joiner's lines are not placed at the beginning.
        self._track_start_ms[track_id] = p.now_ms() - self._clock_start
        self._asr[track_id] = ParticipantASR(
            participant_id,
            self.runner,
            partial_interval_ms=self.partial_interval_ms,
        )
        now = p.now_ms() - self._clock_start
        participant["present"] = True
        if not participant.get("joined_at_ms"):
            participant["joined_at_ms"] = now
        self._participant_state[track_id] = participant
        if self.identity is not None:
            self.identity.mark_present(participant_id, participant["display_name"])
        log.info("participant %s (%s) joined on track %s", participant_id, participant["display_name"], track_id[:8])
        await self._emit(p.ev_participant(now, participant))

        # Always watch for a camera, whatever we saw at join time: people switch
        # one on mid-meeting, and "no video track yet" is not "no camera".
        task = asyncio.create_task(self._face_loop(participant_id, track_id))
        self._face_tasks.append(task)
        self._face_by_participant[participant_id] = task

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

            # Identity is checked on every usable frame, before the "better
            # picture" gate: recognising a returning person must not depend on
            # whether this particular frame is prettier than the last one.
            if (
                self.identity is not None
                and getattr(detection, "embedding", None) is not None
                and await self._observe_face(participant_id, track_id, detection)
            ):
                return  # merged: a new watcher owns this track now

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


    async def _observe_face(self, participant_id: str, track_id: str, detection: Any) -> bool:
        """Ask the identity book about this frame; True when records were merged."""
        assert self.identity is not None
        state = self._participant_state.get(track_id)
        if state is None or detection.confidence < MIN_FACE_CONFIDENCE:
            return False
        box = getattr(detection, "box", None)
        if not box or min(box[2], box[3]) < MIN_IDENTITY_FACE_PX:
            # Too small to recognise: such a frame must never confirm a merge.
            return False
        match = self.identity.observe(
            participant_id,
            detection.embedding,
            name=state.get("display_name"),
            quality=detection.confidence,
        )
        if match is None or not match.confirmed or match.participant_id == participant_id:
            return False
        target = self._by_id.get(match.participant_id)
        if target is None:
            return False
        return await self._merge_records(track_id, participant_id, target, match)

    async def _merge_records(
        self, track_id: str, from_id: str, target: dict[str, Any], match: FaceMatch
    ) -> bool:
        """One person, two records: keep the older one and move the track to it."""
        assert self._emit is not None
        target_id = target["id"]
        if self._live.get(target_id, track_id) != track_id:
            # Both are in the room: a guess here would fuse two people.
            log.info("identity: not merging %s into %s, both are present", from_id, target_id)
            return False

        previous = self._participant_state.get(track_id) or {}
        self._participants[track_id] = target_id
        self._live[target_id] = track_id
        self._live.pop(from_id, None)
        self._participant_state[track_id] = target
        asr = self._asr.get(track_id)
        if asr is not None:
            asr.participant_id = target_id
        self._by_id.pop(from_id, None)
        for name, record in list(self._by_name.items()):
            if record.get("id") == from_id:
                del self._by_name[name]
        if self.identity is not None:
            self.identity.forget(from_id)
            self.identity.mark_present(target_id, target.get("display_name"))

        info = self.agent._tracks.get(track_id)
        # The tile name is the freshest one; the participant's own name is the
        # fallback when the DOM gave us nothing.
        wanted = (getattr(info, "name", "") or previous.get("display_name") or "").strip()
        if wanted and wanted != target.get("display_name"):
            # The person may have renamed since the last visit; the newest name wins.
            self._rename(target, wanted)
            if self.identity is not None:
                self.identity.rename(target_id, target["display_name"])
        target["present"] = True

        at = p.now_ms() - self._clock_start
        await self._emit(p.ev_participant_merged(at, from_id, target_id))
        await self._emit(p.ev_participant(at, target))
        await self._emit(
            p.ev_notice(
                at,
                f"Лицо совпало с «{target['display_name']}» ({match.similarity:.2f}): "
                "это тот же человек, записи объединены.",
            )
        )
        log.info("identity: merged %s into %s (%.3f)", from_id, target_id, match.similarity)
        # The current watcher ends with this function; the merged record gets a
        # fresh one so its face keeps improving.
        self._face_by_participant.pop(from_id, None)
        task = asyncio.create_task(self._face_loop(target_id, track_id))
        self._face_tasks.append(task)
        self._face_by_participant[target_id] = task
        return True

    async def _publish(self, track_id: str, events: list[ASREvent]) -> None:
        assert self._emit is not None
        participant_id = self._participants.get(track_id)
        if participant_id is None:
            return
        offset = self._track_start_ms.get(track_id, 0)
        if events:
            self._spoken.add(participant_id)
        for event in events:
            start_ms = offset + event.start_ms
            end_ms = offset + event.end_ms
            if event.kind == "partial":
                self._stats["partials"] += 1
                await self._emit(
                    p.ev_segment(
                        end_ms,
                        {
                            "id": f"{participant_id}-live",
                            "participant_id": participant_id,
                            "start_ms": start_ms,
                            "end_ms": end_ms,
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
                if previous and previous[0] == normalized and abs(start_ms - previous[1]) <= 2000:
                    # The decoder re-emitted the same phrase for the same words;
                    # keeping both would double every "алло" in the protocol.
                    log.debug("dropping duplicate final for %s: %s", track_id[:8], event.text)
                    continue
                self._last_final[track_id] = (normalized, start_ms)
                self._stats["finals"] += 1
                await self._emit(
                    p.ev_segment(
                        end_ms,
                        {
                            "id": f"{participant_id}-{start_ms}",
                            "participant_id": participant_id,
                            "start_ms": start_ms,
                            "end_ms": end_ms,
                            "text": event.text,
                            "final": True,
                            "confidence": None,
                            "language": event.language,
                        },
                    )
                )


def build_jazz(meeting_url: str, **options: Any) -> JazzPipeline:
    return JazzPipeline(meeting_url, **options)
