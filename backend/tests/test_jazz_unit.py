"""Unit-level checks for the real pipeline object.

The browser parts are covered by the opt-in e2e test; here we pin the wiring that
the API relies on: the pipeline name, the shared model, the diagnostic screenshot
hook (which must never raise when no directory is configured), the duplicate
guard and the participant bookkeeping.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from app import protocol as p
from app.asr import ASREvent
from app.capture import ParticipantUpdate
from app.jazz import JazzPipeline


class DeadAgent:
    """A browser that joined and then died — the classic frozen-demo failure."""

    def __init__(self, reason: str = "page crashed") -> None:
        self.notes: list[str] = []
        self._tracks: dict[str, Any] = {}
        self.crashed = True
        self.crash_reason = reason
        self.human_present = False
        self.updates: asyncio.Queue[ParticipantUpdate] = asyncio.Queue()

    async def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return False

    async def chunks(self):
        if False:  # pragma: no cover - makes this an async generator, like the real one
            yield None
        while True:  # noqa: ASYNC110 - a fake stream that never produces anything
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        return None

    async def screenshot(self, path: str) -> None:
        return None


def make_pipeline(**options: Any) -> JazzPipeline:
    return JazzPipeline("https://salutejazz.ru/calls/abc123", agent=DeadAgent(), **options)


def collect(pipeline: JazzPipeline) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    pipeline._emit = emit
    pipeline._clock_start = 0
    return events


def test_pipeline_identity() -> None:
    pipeline = JazzPipeline("https://jazz.sber.ru/meeting/x")
    assert pipeline.name == "jazz"
    assert pipeline.runner is not None
    assert pipeline.detector.name in {"yunet", "insightface", "none"}


def test_artifacts_dir_is_optional() -> None:
    pipeline = JazzPipeline("https://jazz.sber.ru/meeting/x")
    assert asyncio.run(pipeline._screenshot("test")) is None


def test_artifacts_dir_can_be_set() -> None:
    from pathlib import Path

    pipeline = JazzPipeline("https://jazz.sber.ru/meeting/x")
    pipeline.set_artifacts_dir(Path("data/some-session"))
    assert pipeline._artifacts_dir == Path("data/some-session")


def test_model_is_shared_between_pipelines() -> None:
    first = JazzPipeline("https://jazz.sber.ru/a")
    second = JazzPipeline("https://jazz.sber.ru/b")
    # One 1.6 GB model per meeting would make back-to-back demos feel broken.
    assert first.runner is second.runner


def test_pipeline_fails_fast_when_the_browser_dies() -> None:
    """A closed page must end the session, not hang until someone clicks stop."""
    pipeline = JazzPipeline("https://jazz.sber.ru/meeting/x", agent=DeadAgent())
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="браузер"):
            await asyncio.wait_for(pipeline.run(emit, None), timeout=5)

    asyncio.run(scenario())

    notices = [e["message"] for e in events if e["type"] == "notice"]
    assert any("Браузер агента закрылся" in message for message in notices), notices


def test_pipeline_explains_a_join_without_tracks() -> None:
    pipeline = JazzPipeline("https://jazz.sber.ru/meeting/x", agent=DeadAgent("page closed"))
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(pipeline.run(emit, None), timeout=5)

    asyncio.run(scenario())
    notices = [e["message"] for e in events if e["type"] == "notice"]
    assert any("медиапотоков нет" in message for message in notices), notices


# -- duplicates -------------------------------------------------------------


def test_duplicate_final_is_dropped() -> None:
    """The decoder sometimes finalizes the same phrase twice; keep one."""
    pipeline = make_pipeline()
    events = collect(pipeline)
    pipeline._participants["track-1"] = "p-1"

    phrase = ASREvent(kind="final", text="Алло.", start_ms=1000, end_ms=1400)
    repeat = ASREvent(kind="final", text="Алло.", start_ms=1100, end_ms=1500)

    asyncio.run(pipeline._publish("track-1", [phrase, repeat]))
    finals = [e for e in events if e["type"] == "segment"]

    assert len(finals) == 1
    assert pipeline._stats["finals"] == 1


def test_a_real_repetition_is_kept() -> None:
    """Saying the same word again after a pause is speech, not a duplicate."""
    pipeline = make_pipeline()
    events = collect(pipeline)
    pipeline._participants["track-1"] = "p-1"

    first = ASREvent(kind="final", text="Да.", start_ms=1000, end_ms=1400)
    later = ASREvent(kind="final", text="Да.", start_ms=9000, end_ms=9400)

    asyncio.run(pipeline._publish("track-1", [first, later]))

    assert len([e for e in events if e["type"] == "segment"]) == 2


# -- participants -----------------------------------------------------------


def test_rename_is_emitted_for_a_late_joining_guest() -> None:
    pipeline = make_pipeline()
    events = collect(pipeline)
    pipeline._participants["track-1"] = "p-1"
    pipeline._participant_state["track-1"] = p.new_participant("p-1", "Участник 2")

    asyncio.run(pipeline._apply_participant_update(ParticipantUpdate("track-1", name="Адель")))

    updates = [e["participant"] for e in events if e["type"] == "participant"]
    assert updates[-1]["display_name"] == "Адель"
    assert updates[-1]["id"] == "p-1"


def test_departure_clears_speaking_and_keeps_the_participant() -> None:
    pipeline = make_pipeline()
    events = collect(pipeline)
    pipeline._participants["track-1"] = "p-1"
    state = p.new_participant("p-1", "Роман")
    state["speaking"] = True
    pipeline._participant_state["track-1"] = state

    asyncio.run(pipeline._apply_participant_update(ParticipantUpdate("track-1", present=False)))

    updates = [e["participant"] for e in events if e["type"] == "participant"]
    assert updates[-1] == {"id": "p-1", "present": False, "speaking": False}
    # The event is a patch: the name is not resent, so it cannot be reset.
    assert "display_name" not in updates[-1]


def test_unchanged_update_emits_nothing() -> None:
    pipeline = make_pipeline()
    events = collect(pipeline)
    pipeline._participants["track-1"] = "p-1"
    pipeline._participant_state["track-1"] = p.new_participant("p-1", "Роман")

    asyncio.run(pipeline._apply_participant_update(ParticipantUpdate("track-1", name="Роман")))
    asyncio.run(pipeline._apply_participant_update(ParticipantUpdate("track-1", present=True)))

    assert [e for e in events if e["type"] == "participant"] == []


def test_unknown_track_update_is_ignored() -> None:
    pipeline = make_pipeline()
    events = collect(pipeline)

    asyncio.run(pipeline._apply_participant_update(ParticipantUpdate("nope", name="Адель")))

    assert events == []


# -- empty room -------------------------------------------------------------


def test_empty_room_finishes_the_meeting_after_the_grace_period() -> None:
    pipeline = make_pipeline(empty_room_timeout_s=1)
    events = collect(pipeline)
    pipeline._participants["track-1"] = "p-1"
    pipeline._participant_state["track-1"] = p.new_participant("p-1", "Роман")
    pipeline._saw_human = True
    pipeline.agent.human_present = False

    async def scenario() -> tuple[bool, bool]:
        first = await pipeline._room_is_empty(0)  # starts the clock
        await asyncio.sleep(1.05)
        second = await pipeline._room_is_empty(1000)
        return first, second

    first, second = asyncio.run(scenario())

    assert first is False, "the grace period must be honoured"
    assert second is True
    assert any("не осталось участников" in e["message"] for e in events)


def test_empty_room_is_ignored_before_anyone_joined() -> None:
    pipeline = make_pipeline(empty_room_timeout_s=1)
    collect(pipeline)
    pipeline._participants["track-1"] = "p-1"
    pipeline._participant_state["track-1"] = p.new_participant("p-1", "Роман")

    assert asyncio.run(pipeline._room_is_empty(0)) is False


def test_empty_room_check_can_be_disabled() -> None:
    pipeline = make_pipeline(empty_room_timeout_s=0)
    collect(pipeline)
    pipeline._participants["track-1"] = "p-1"
    pipeline._participant_state["track-1"] = p.new_participant("p-1", "Роман")
    pipeline._saw_human = True

    assert asyncio.run(pipeline._room_is_empty(0)) is False


# -- faces ------------------------------------------------------------------


class FrameAgent(DeadAgent):
    """Serves a scripted sequence of frames, and records which track was asked."""

    def __init__(self, frames: list[str | None]) -> None:
        super().__init__()
        self.frames = list(frames)
        self.asked: list[str] = []

    async def grab_face(self, track_id: str, max_width: int = 320) -> str | None:
        self.asked.append(track_id)
        return self.frames.pop(0) if self.frames else None


def test_face_loop_reports_a_frame_when_the_camera_turns_on_later() -> None:
    """The camera may appear after the audio track; the loop must keep asking."""
    agent = FrameAgent([None, "data:image/jpeg;base64,AAAA"])
    pipeline = JazzPipeline("https://salutejazz.ru/calls/abc123", agent=agent)
    events = collect(pipeline)

    asyncio.run(pipeline._face_loop("p-1", "track-1", delays=(0, 0, 0)))

    faces = [e for e in events if e["type"] == "face"]
    assert len(faces) == 1
    assert faces[0]["status"] == "frame"
    assert set(agent.asked) == {"track-1"}, "the audio track id is the lookup key"
    assert len(agent.asked) == 3, "the watcher keeps going, it does not stop at the first frame"


def test_face_loop_reports_no_camera_after_every_attempt() -> None:
    agent = FrameAgent([None, None, None])
    pipeline = JazzPipeline("https://salutejazz.ru/calls/abc123", agent=agent)
    events = collect(pipeline)

    asyncio.run(pipeline._face_loop("p-1", "track-1", delays=(0, 0, 0), absence_after_s=0))

    faces = [e for e in events if e["type"] == "face"]
    assert [f["status"] for f in faces] == ["none"]
    assert len(agent.asked) == 3


def test_patch_merges_into_the_participant(tmp_path) -> None:
    """A rename must not erase the face found earlier — the session merges patches."""
    from pathlib import Path

    from app.mock import MockPipeline
    from app.session import Session

    session = Session(
        "abc123",
        "https://salutejazz.ru/calls/abc123",
        MockPipeline("https://salutejazz.ru/calls/abc123"),
        p.now_ms(),
        data_dir=Path(tmp_path),
    )
    session.apply(
        p.ev_participant(0, {"id": "p-1", "display_name": "Участник 2", "face_status": "detected", "role": "human"})
    )
    session.apply(p.ev_participant(10, {"id": "p-1", "display_name": "Адель"}))
    session.apply(p.ev_participant(20, {"id": "p-1", "present": False}))

    participant = session.participants["p-1"]
    assert participant["display_name"] == "Адель"
    assert participant["face_status"] == "detected"
    assert participant["present"] is False
    assert participant["role"] == "human"


def test_video_track_arriving_later_is_accepted() -> None:
    """A camera switched on mid-call sends a second message for the same track."""
    from app.capture import BrowserAgent

    agent = BrowserAgent("https://salutejazz.ru/calls/abc123")
    agent._on_track(None, '{"key": "track-1", "name": "Гость"}')
    assert agent._tracks["track-1"].video_track_id is None

    agent._on_track(None, '{"key": "track-1", "name": "Гость", "videoTrackId": "TR_VIDEO"}')

    assert agent._tracks["track-1"].video_track_id == "TR_VIDEO"
    assert len(agent._tracks) == 1, "the second message must not create a duplicate"


def test_face_watcher_starts_even_without_a_video_track() -> None:
    """No video at join time is not "no camera": the watcher must keep looking."""
    agent = FrameAgent([])
    pipeline = JazzPipeline("https://salutejazz.ru/calls/abc123", agent=agent)
    collect(pipeline)
    pipeline._clock_start = 0
    pipeline._participants["track-1"] = "p-1"

    async def scenario() -> None:
        await pipeline._register_track("track-1", "Гость")
        tasks = list(pipeline._face_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())

    assert pipeline._face_tasks, "a watcher task must be scheduled"


def test_no_detector_is_not_reported_as_missing_camera() -> None:
    """Without a detector we still report a frame, never "без камеры"."""
    agent = FrameAgent(["data:image/jpeg;base64,AAAA"])
    pipeline = JazzPipeline("https://salutejazz.ru/calls/abc123", agent=agent)
    pipeline.detector = type("NoDetector", (), {"name": "none", "detect": lambda self, frame: None})()
    events = collect(pipeline)

    asyncio.run(pipeline._face_loop("p-1", "track-1", delays=(0,)))

    faces = [e for e in events if e["type"] == "face"]
    assert [f["status"] for f in faces] == ["frame"]


def test_face_watcher_stops_when_the_participant_leaves() -> None:
    """Polling a dead track for the rest of the meeting is pure waste."""
    pipeline = make_pipeline()
    collect(pipeline)
    pipeline._participants["track-1"] = "p-1"
    pipeline._participant_state["track-1"] = p.new_participant("p-1", "Роман")

    async def scenario() -> asyncio.Task:
        watcher = asyncio.create_task(asyncio.sleep(3600))
        pipeline._face_by_participant["p-1"] = watcher
        await pipeline._apply_participant_update(ParticipantUpdate("track-1", present=False))
        await asyncio.sleep(0)
        return watcher

    watcher = asyncio.run(scenario())

    assert watcher.cancelled()
    assert "p-1" not in pipeline._face_by_participant


def test_pipeline_language_is_configurable() -> None:
    """A pinned language must reach the shared runner, not be dropped on the way."""
    from app.jazz import JazzPipeline

    pipeline = JazzPipeline("https://salutejazz.ru/calls/abc123", language="en")

    assert pipeline.language == "en"
    assert pipeline.runner.language == "en"


def test_late_name_update_is_applied() -> None:
    """The tile can be painted after the track: the name must still land."""
    from app.capture import BrowserAgent

    agent = BrowserAgent("https://salutejazz.ru/calls/abc123")
    agent._on_track(None, '{"key": "track-1", "name": "Участник 1"}')

    agent._on_track(None, '{"key": "track-1", "name": "Анна Соколова"}')

    assert agent._tracks["track-1"].name == "Анна Соколова"
    update = agent.updates.get_nowait()
    assert update.track_id == "track-1" and update.name == "Анна Соколова"


def test_a_silent_participant_produces_a_notice() -> None:
    """The operator must see that one track is silent, not guess it."""
    pipeline = make_pipeline()
    events = collect(pipeline)
    pipeline._participant_state["track-1"] = p.new_participant("p-1", "Анна Соколова")

    asyncio.run(
        pipeline._apply_participant_update(
            ParticipantUpdate("track-1", alert="нет звука: браузер не отдаёт дорожку участника")
        )
    )

    notices = [event["message"] for event in events if event["type"] == "notice"]
    assert any("нет звука" in message and "Анна Соколова" in message for message in notices)
