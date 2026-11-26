"""The face in a tile may improve over time, but must never get worse.

Before this, the first usable frame won forever: one bad moment — a turned head,
a blurry frame, a 40% detection — stayed in the tile for the whole meeting.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.jazz import JazzPipeline


class FrameAgent:
    """Serves scripted frames; records how many times it was asked."""

    def __init__(self, frames: list[str | None]) -> None:
        self.frames = list(frames)
        self.asked = 0
        self._tracks: dict[str, Any] = {}
        self.human_present = True
        self.updates: asyncio.Queue = asyncio.Queue()

    async def grab_face(self, track_id: str, max_width: int = 320) -> str | None:  # noqa: ARG002
        self.asked += 1
        return self.frames.pop(0) if self.frames else None


class ScriptedDetector:
    """Returns the confidences it was given, in order."""

    name = "scripted"

    def __init__(self, confidences: list[float | None]) -> None:
        self.confidences = list(confidences)

    def detect(self, frame: str) -> Any | None:  # noqa: ARG002
        value = self.confidences.pop(0) if self.confidences else None
        if value is None:
            return None
        return type(
        "Detection",
        (),
        {"confidence": value, "thumbnail": f"thumb-{value}", "embedding": None},
    )()


def pipeline_with(frames: list[str | None], confidences: list[float | None]) -> tuple[JazzPipeline, FrameAgent, list]:
    agent = FrameAgent(frames)
    pipeline = JazzPipeline("https://salutejazz.ru/calls/abc123", agent=agent)  # type: ignore[arg-type]
    pipeline.detector = ScriptedDetector(confidences)  # type: ignore[assignment]
    pipeline._clock_start = 0
    events: list = []

    async def emit(event: dict) -> None:
        events.append(event)

    pipeline._emit = emit
    return pipeline, agent, events


def faces(events: list) -> list[dict]:
    return [event for event in events if event["type"] == "face"]


def test_better_face_replaces_the_weaker_one() -> None:
    pipeline, agent, events = pipeline_with(
        ["f1", "f2", "f3"], [0.55, 0.72, 0.91]
    )

    asyncio.run(pipeline._face_loop("p-1", "track-1", delays=(0, 0, 0)))

    confidences = [face["confidence"] for face in faces(events)]
    assert confidences == [0.55, 0.72, 0.91]
    assert agent.asked == 3


def test_a_worse_face_never_replaces_a_better_one() -> None:
    pipeline, _agent, events = pipeline_with(["f1", "f2", "f3"], [0.9, 0.4, 0.85])

    asyncio.run(pipeline._face_loop("p-1", "track-1", delays=(0, 0, 0)))

    confidences = [face["confidence"] for face in faces(events)]
    assert confidences == [0.9], "0.4 and 0.85 are not improvements over 0.9"


def test_small_improvement_is_ignored() -> None:
    """A 0.02 gain is noise, not a better picture; it would only churn the tile."""
    pipeline, _agent, events = pipeline_with(["f1", "f2"], [0.7, 0.72])

    asyncio.run(pipeline._face_loop("p-1", "track-1", delays=(0, 0)))

    assert len(faces(events)) == 1


def test_a_frame_is_replaced_by_a_detected_face() -> None:
    pipeline, _agent, events = pipeline_with(["f1", "f2"], [None, 0.66])

    asyncio.run(pipeline._face_loop("p-1", "track-1", delays=(0, 0)))

    statuses = [face["status"] for face in faces(events)]
    assert statuses == ["frame", "detected"]


def test_a_frame_is_not_replaced_by_another_frame() -> None:
    pipeline, _agent, events = pipeline_with(["f1", "f2"], [None, None])

    asyncio.run(pipeline._face_loop("p-1", "track-1", delays=(0, 0)))

    assert [face["status"] for face in faces(events)] == ["frame"]


def test_no_camera_is_announced_only_when_nothing_arrived() -> None:
    pipeline, _agent, events = pipeline_with([None, None, None], [])

    asyncio.run(pipeline._face_loop("p-1", "track-1", delays=(0, 0, 0), absence_after_s=0))

    assert [face["status"] for face in faces(events)] == ["none"]


def test_a_frame_cancels_the_no_camera_notice() -> None:
    """Once a frame has been seen, "без камеры" must never be announced."""
    pipeline, _agent, events = pipeline_with(["f1", None], [None])

    asyncio.run(pipeline._face_loop("p-1", "track-1", delays=(0, 0), absence_after_s=0))

    statuses = [face["status"] for face in faces(events)]
    assert statuses == ["frame"], statuses
