"""Session state, event fan-out and JSONL persistence.

Design notes
------------
* One ``Session`` owns one ``Pipeline``. The pipeline is the only component that
  knows *how* audio is captured and transcribed; the session only records what
  the pipeline reports. Swapping the mock pipeline for the real
  Playwright + Whisper one therefore touches nothing else.
* Every event is written to ``events.jsonl`` *before* it is broadcast, so a
  crash mid-demo still leaves a complete, replayable transcript on disk.
* Replay rebuilds participants and segments, which is what export uses after a
  restart. The log is the source of truth, not in-memory state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from . import protocol as p

log = logging.getLogger(__name__)

Emitter = Callable[[dict[str, Any]], Awaitable[None]]


class Pipeline(Protocol):
    """Produces protocol events for one session.

    Implementations must be cancellation-safe: ``run`` may be cancelled when
    the session is stopped, and must not leave background tasks behind.
    """

    name: str

    async def run(self, emit: Emitter, deadline_ms: int | None) -> None: ...


class Session:
    def __init__(
        self,
        session_id: str,
        meeting_url: str,
        pipeline: Pipeline,
        started_at_ms: int,
        *,
        title: str | None = None,
        duration_min: int | None = None,
        start_at_ms: int | None = None,
        options: dict[str, Any] | None = None,
        data_dir: Path,
    ) -> None:
        self.id = session_id
        self.meeting_url = meeting_url
        self.title = title or "Встреча"
        self.pipeline = pipeline
        self.started_at_ms = started_at_ms
        self.created_at_ms = started_at_ms
        self.start_at_ms = start_at_ms
        self.options = options or {}
        self.duration_min = duration_min
        self.dir = data_dir / session_id

        self.state: str = p.CONNECTING
        self.error: str | None = None
        self.participants: dict[str, dict[str, Any]] = {}
        self.segments: dict[str, dict[str, Any]] = {}

        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._task: asyncio.Task[None] | None = None
        self._event_log: list[dict[str, Any]] = []

    # -- persistence ---------------------------------------------------------

    def _write_event(self, event: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _write_meta(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": self.id,
            "title": self.title,
            "meeting_url": self.meeting_url,
            "pipeline": self.pipeline.name,
            "started_at_ms": self.started_at_ms,
            "created_at_ms": self.created_at_ms,
            "start_at_ms": self.start_at_ms,
            "duration_min": self.duration_min,
            "options": self.options,
            "state": self.state,
            "error": self.error,
        }
        (self.dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- state projection ----------------------------------------------------

    def apply(self, event: dict[str, Any]) -> None:
        """Fold one event into the current state."""
        kind = event["type"]
        if kind == "session":
            self.state = event["state"]
            if event.get("error"):
                self.error = event["error"]
        elif kind == "participant":
            part = event["participant"]
            self.participants[part["id"]] = dict(part)
        elif kind == "speech":
            part = self.participants.get(event["participant_id"])
            if part is not None:
                part["speaking"] = event["speaking"]
        elif kind == "face":
            part = self.participants.get(event["participant_id"])
            if part is not None:
                part["face_status"] = event["status"]
                if "confidence" in event:
                    part["face_confidence"] = event["confidence"]
                if "thumbnail" in event:
                    part["thumbnail"] = event["thumbnail"]
        elif kind == "segment":
            self.segments[event["segment"]["id"]] = dict(event["segment"])

    def final_segments(self) -> list[dict[str, Any]]:
        items = [s for s in self.segments.values() if s.get("final")]
        items.sort(key=lambda s: (s["start_ms"], s["id"]))
        return items

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "session": self.as_dict(),
            "participants": sorted(
                self.participants.values(), key=lambda x: x["joined_at_ms"]
            ),
            "segments": self.final_segments(),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "meeting_url": self.meeting_url,
            "pipeline": self.pipeline.name,
            "state": self.state,
            "error": self.error,
            "started_at_ms": self.started_at_ms,
            "created_at_ms": self.created_at_ms,
            "start_at_ms": self.start_at_ms,
            "duration_min": self.duration_min,
            "participants_count": len(self.participants),
            "segments_count": len(self.final_segments()),
        }

    def _at(self) -> int:
        return p.now_ms() - self.started_at_ms

    # -- fan-out -------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    async def emit(self, event: dict[str, Any]) -> None:
        self.apply(event)
        self._write_event(event)
        if event["type"] in ("session", "done"):
            self._write_meta()
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow browser must not stall the pipeline: drop the oldest
                # event, the UI re-syncs from the next snapshot.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(event)

    # -- lifecycle -----------------------------------------------------------

    async def start(self, *, speed: float = 1.0) -> None:
        self._task = asyncio.create_task(self._run(speed), name=f"session-{self.id}")

    async def _run(self, speed: float) -> None:
        """Wait for ``start_at`` (the "время" from the brief), then go live.

        The wait lives inside the same try block as the pipeline, so the ordinary
        ``stop`` endpoint cancels a scheduled meeting the same way it stops a live
        one — including the final state transition.
        """
        self._write_meta()
        deadline_ms = None if not self.duration_min else self.duration_min * 60_000
        try:
            if self.start_at_ms is not None and self.start_at_ms - p.now_ms() > 1000:
                self.state = p.SCHEDULED
                await self.emit(p.ev_session(p.SCHEDULED, self._at(), start_at_ms=self.start_at_ms))
                delay = (self.start_at_ms - p.now_ms()) / 1000
                log.info("session %s scheduled in %.0f s", self.id, delay)
                await asyncio.sleep(delay)
                # Transcript timestamps start at the real join, not at booking time.
                self.started_at_ms = p.now_ms()

            await self.emit(p.ev_session(p.CONNECTING, self._at()))
            await self.emit(p.ev_session(p.LIVE, self._at()))
            await self.pipeline.run(self.emit, deadline_ms)
            await self.emit(p.ev_done(self._at(), "completed"))
            await self.emit(p.ev_session(p.FINISHED, self._at()))
        except asyncio.CancelledError:
            await self.emit(p.ev_done(self._at(), "stopped"))
            await self.emit(p.ev_session(p.FINISHED, self._at()))
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI on purpose
            log.exception("session %s failed", self.id)
            await self.emit(p.ev_session(p.FAILED, self._at(), error=str(exc)))

    async def stop(self) -> None:
        if self._task is None or self._task.done():
            return
        self.state = p.STOPPING
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


class SessionManager:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    def create(
        self,
        meeting_url: str,
        *,
        title: str | None,
        duration_min: int | None,
        pipeline: Pipeline,
        start_at_ms: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> Session:
        session_id = uuid.uuid4().hex[:12]
        session = Session(
            session_id,
            meeting_url,
            pipeline,
            p.now_ms(),
            title=title,
            duration_min=duration_min,
            start_at_ms=start_at_ms,
            options=options,
            data_dir=self.data_dir,
        )
        self.sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def all(self) -> list[Session]:
        return sorted(self.sessions.values(), key=lambda s: s.created_at_ms, reverse=True)

    def due_scheduled(self) -> list[Session]:
        """Sessions booked for the future that must be re-armed after a restart."""
        return [
            session
            for session in self.sessions.values()
            if session.state == p.SCHEDULED
            and session.start_at_ms is not None
            and session.start_at_ms > p.now_ms()
        ]

    # -- restore after restart ----------------------------------------------

    def load_from_disk(self) -> int:
        """Rebuild finished/live sessions by replaying their event logs."""
        if not self.data_dir.exists():
            return 0
        loaded = 0
        for session_dir in sorted(self.data_dir.iterdir()):
            meta_path = session_dir / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                session = Session(
                    meta["id"],
                    meta["meeting_url"],
                    _RestoredPipeline(meta.get("pipeline", "unknown")),
                    meta["started_at_ms"],
                    title=meta.get("title"),
                    duration_min=meta.get("duration_min"),
                    start_at_ms=meta.get("start_at_ms"),
                    options=meta.get("options") or {},
                    data_dir=self.data_dir,
                )
                for line in (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        session.apply(json.loads(line))
                still_booked = (
                    session.state == p.SCHEDULED
                    and session.start_at_ms is not None
                    and session.start_at_ms > p.now_ms()
                )
                if session.state not in p.TERMINAL_STATES and not still_booked:
                    # The process died mid-session; nothing can resume it.
                    session.state = p.FAILED
                    session.error = session.error or "прервано перезапуском сервера"
                self.sessions[session.id] = session
                loaded += 1
            except Exception:  # noqa: BLE001 - a broken folder must not block startup
                log.warning("skipping unreadable session dir %s", session_dir, exc_info=True)
        return loaded


class _RestoredPipeline:
    """Placeholder for a session that is only read back from disk."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, emit: Emitter, deadline_ms: int | None) -> None:  # pragma: no cover
        raise RuntimeError("restored sessions cannot be run")
