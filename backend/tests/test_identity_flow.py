"""The pipeline side of face identity: recognising a returning person.

Unit rules live in ``test_identity_match``; this file checks that the pipeline
actually merges the records, keeps the lines, and never puts a biometric vector
into the event log.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from app import protocol as p
from app.capture import ParticipantUpdate
from app.identity import IdentityBook
from app.jazz import JazzPipeline
from app.session import Session

ALICE = (1.0, 0.0, 0.0, 0.0)
ALICE_AGAIN = (0.98, 0.08, 0.0, 0.0)
BOB = (0.0, 1.0, 0.0, 0.0)


class FakeAgent:
    def __init__(self) -> None:
        self._tracks: dict[str, Any] = {}
        self.updates: asyncio.Queue[ParticipantUpdate] = asyncio.Queue()

    def is_alive(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def make_pipeline() -> tuple[JazzPipeline, list[dict[str, Any]]]:
    pipeline = JazzPipeline("https://salutejazz.ru/calls/abc123", agent=FakeAgent())
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    pipeline._emit = emit
    pipeline._clock_start = p.now_ms()
    pipeline.identity = IdentityBook(confirmations=2)
    return pipeline, events


def face(
    vector: tuple[float, ...], confidence: float = 0.9, size: int = 200
) -> Any:
    return SimpleNamespace(
        confidence=confidence, embedding=vector, thumbnail=None, box=(10, 10, size, size)
    )


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def merged(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == "participant_merged"]


def test_a_returning_person_is_recognised_by_face() -> None:
    async def scenario() -> tuple[JazzPipeline, list[dict[str, Any]], list[bool]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._observe_face("p-1", "t1", face(ALICE))  # remembers the face
        await pipeline._observe_face("p-1", "t1", face(ALICE_AGAIN))
        await pipeline._apply_participant_update(ParticipantUpdate("t1", present=False))

        # She comes back under a different display name, same face.
        await pipeline._register_track("t2", "Адельы")
        results = [
            await pipeline._observe_face("p-2", "t2", face(ALICE)),
            await pipeline._observe_face("p-2", "t2", face(ALICE_AGAIN)),
        ]
        return pipeline, events, results

    pipeline, events, results = run(scenario())

    assert results == [False, True], "the first frame waits, the second merges"
    assert pipeline._participants["t2"] == "p-1", "one person, one record"
    assert "p-2" not in pipeline._by_id

    assert merged(events) == [{"type": "participant_merged", "at_ms": merged(events)[0]["at_ms"], "from_id": "p-2", "to_id": "p-1"}]

    record = [event["participant"] for event in events if event["type"] == "participant"][-1]
    assert record["id"] == "p-1"
    assert record["display_name"] == "Адельы", "the newest display name wins"


def test_the_merge_is_explained_to_the_operator() -> None:
    async def scenario() -> list[dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._observe_face("p-1", "t1", face(ALICE))
        await pipeline._apply_participant_update(ParticipantUpdate("t1", present=False))
        await pipeline._register_track("t2", "Адельы")
        await pipeline._observe_face("p-2", "t2", face(ALICE))
        await pipeline._observe_face("p-2", "t2", face(ALICE_AGAIN))
        return events

    events = run(scenario())
    notices = [event["message"] for event in events if event["type"] == "notice"]

    assert any("Лицо совпало" in message and "объединены" in message for message in notices)


def test_two_people_in_the_room_are_not_merged() -> None:
    async def scenario() -> list[bool]:
        pipeline, _events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._observe_face("p-1", "t1", face(ALICE))
        # A second person with the same face joins while the first is still here.
        await pipeline._register_track("t2", "Двойник")
        return [
            await pipeline._observe_face("p-2", "t2", face(ALICE)),
            await pipeline._observe_face("p-2", "t2", face(ALICE_AGAIN)),
        ]

    assert run(scenario()) == [False, False]


def test_a_different_face_is_a_different_person() -> None:
    async def scenario() -> list[dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._observe_face("p-1", "t1", face(ALICE))
        await pipeline._apply_participant_update(ParticipantUpdate("t1", present=False))
        await pipeline._register_track("t2", "Борис")
        await pipeline._observe_face("p-2", "t2", face(BOB))
        await pipeline._observe_face("p-2", "t2", face(BOB))
        return events

    events = run(scenario())

    assert merged(events) == [], "different faces must never be fused"


def test_a_low_confidence_frame_never_merges() -> None:
    async def scenario() -> list[bool]:
        pipeline, _events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._observe_face("p-1", "t1", face(ALICE))
        await pipeline._apply_participant_update(ParticipantUpdate("t1", present=False))
        await pipeline._register_track("t2", "Адельы")
        return [
            await pipeline._observe_face("p-2", "t2", face(ALICE, confidence=0.5)),
            await pipeline._observe_face("p-2", "t2", face(ALICE_AGAIN, confidence=0.5)),
        ]

    assert run(scenario()) == [False, False]


def test_a_tiny_face_never_merges() -> None:
    """An 80 px crop is too coarse for identity, however good the vector looks."""

    async def scenario() -> list[bool]:
        pipeline, _events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._observe_face("p-1", "t1", face(ALICE))
        await pipeline._apply_participant_update(ParticipantUpdate("t1", present=False))
        await pipeline._register_track("t2", "Адельы")
        return [
            await pipeline._observe_face("p-2", "t2", face(ALICE, size=40)),
            await pipeline._observe_face("p-2", "t2", face(ALICE_AGAIN, size=40)),
        ]

    assert run(scenario()) == [False, False]


def test_embeddings_never_reach_the_event_log() -> None:
    """A biometric template at rest is a different object from a photo: keep it out."""

    async def scenario() -> list[dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._observe_face("p-1", "t1", face(ALICE))
        return events

    events = run(scenario())

    assert not any("embedding" in str(event) for event in events)


def test_merging_keeps_every_line(tmp_path) -> None:
    """A placeholder that already spoke must hand its words to the real record."""
    session = Session(
        "abc123abc123", "https://salutejazz.ru/calls/abc123", None, p.now_ms(), data_dir=tmp_path
    )
    session.apply(p.ev_participant(0, p.new_participant("p-1", "Адель")))
    session.apply(p.ev_participant(0, p.new_participant("p-2", "Участник 2")))
    session.apply(
        p.ev_segment(
            1000,
            {
                "id": "p-2-0",
                "participant_id": "p-2",
                "start_ms": 0,
                "end_ms": 1000,
                "text": "привет",
                "final": True,
            },
        )
    )

    session.apply(p.ev_participant_merged(2000, "p-2", "p-1"))

    assert "p-2" not in session.participants, "the spare record is gone"
    assert [segment["participant_id"] for segment in session.segments.values()] == ["p-1"]
    assert len(session.final_segments()) == 1, "no words were lost"
