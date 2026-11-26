"""Who said what, when people come and go under the same or different names.

The reported failure: leaving as "Адель", returning as "Адельы" and coming back
again as "Адель" produced lines for the wrong person and a brand-new tile called
"Участник 3". The room only gives names, so the name has to be the identity —
but a name must be freed when the person leaves, and a genuinely different name
must not inherit somebody else's transcript.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app import protocol as p
from app.asr import ASREvent
from app.capture import ParticipantUpdate
from app.jazz import JazzPipeline


class FakeAgent:
    """A browser that has joined: only its track list matters here."""

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
    return pipeline, events


def participants(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Last known state per participant id, as the session would merge it."""
    merged: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["type"] == "participant_removed":
            merged.pop(event["participant_id"], None)
            continue
        if event["type"] != "participant":
            continue
        record = event["participant"]
        merged.setdefault(record["id"], {}).update(record)
    return merged


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def test_the_reported_sequence_keeps_every_person_right() -> None:
    """Адель → вышла → Адельы → вышла → Адель снова: три дорожки, два человека."""

    async def scenario() -> list[dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._apply_participant_update(ParticipantUpdate("t1", present=False))

        await pipeline._register_track("t2", "Адельы")
        await pipeline._apply_participant_update(ParticipantUpdate("t2", present=False))

        await pipeline._register_track("t3", "Адель")
        return events

    events = run(scenario())
    people = participants(events)

    names = {record["display_name"] for record in people.values()}
    assert names == {"Адель", "Адельы"}
    assert len(people) == 2, f"expected two people, got {names}"

    by_name = {record["display_name"]: record["id"] for record in people.values()}
    assert pipeline_ids(events) == [by_name["Адель"], by_name["Адельы"], by_name["Адель"]]


def pipeline_ids(events: list[dict[str, Any]]) -> list[str]:
    """Ids of full participant records, in the order they were registered."""
    return [
        event["participant"]["id"]
        for event in events
        if event["type"] == "participant" and "display_name" in event["participant"]
    ]


def test_nobody_is_called_a_placeholder_when_the_name_is_known() -> None:
    async def scenario() -> list[dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._apply_participant_update(ParticipantUpdate("t1", present=False))
        await pipeline._register_track("t2", "Адель")
        return events

    events = run(scenario())

    names = [people["display_name"] for people in participants(events).values()]
    assert all("Участник" not in name for name in names), names


def test_a_rejoin_continues_the_same_transcript() -> None:
    """The same name is the same person: one id, one history, back to present."""

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._apply_participant_update(ParticipantUpdate("t1", present=False))
        await pipeline._register_track("t2", "Адель")
        return pipeline, participants(events)

    pipeline, people = run(scenario())

    assert len(people) == 1
    record = next(iter(people.values()))
    assert record["present"] is True
    assert pipeline._live == {record["id"]: "t2"}


def test_two_people_with_one_name_are_not_merged() -> None:
    """They cannot be told apart, so the second gets an honest suffix."""

    async def scenario() -> dict[str, dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._register_track("t2", "Адель")
        return participants(events)

    people = run(scenario())

    names = sorted(record["display_name"] for record in people.values())
    assert names == ["Адель", "Адель (2)"]


def test_a_rename_in_place_keeps_the_history() -> None:
    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._apply_participant_update(ParticipantUpdate("t1", name="Адельы"))
        return pipeline, participants(events)

    pipeline, people = run(scenario())

    assert len(people) == 1, "a rename must not create a second person"
    record = next(iter(people.values()))
    assert record["display_name"] == "Адельы"
    assert pipeline._by_name["Адельы"]["id"] == record["id"]


def test_a_freed_name_can_be_taken_by_the_next_person() -> None:
    """After a rename, the old name belongs to nobody: a new Адель is a new person."""

    async def scenario() -> dict[str, dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._apply_participant_update(ParticipantUpdate("t1", name="Адельы"))
        await pipeline._apply_participant_update(ParticipantUpdate("t1", present=False))
        await pipeline._register_track("t2", "Адель")
        return participants(events)

    people = run(scenario())

    assert sorted(record["display_name"] for record in people.values()) == ["Адель", "Адельы"]


def test_placeholders_never_collide() -> None:
    async def scenario() -> dict[str, dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "")
        await pipeline._apply_participant_update(ParticipantUpdate("t1", present=False))
        await pipeline._register_track("t2", "")
        return participants(events)

    people = run(scenario())

    names = sorted(record["display_name"] for record in people.values())
    assert names == ["Участник 1", "Участник 2"]


def test_segments_are_timestamped_in_session_time() -> None:
    """A late joiner's lines must not be placed at the start of the meeting."""

    async def scenario() -> list[dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Анна")
        await pipeline._register_track("t2", "Борис")
        # Anna started at 0 s, Boris at 30 s; both spoke for two seconds.
        pipeline._track_start_ms["t1"] = 0
        pipeline._track_start_ms["t2"] = 30_000
        await pipeline._publish("t1", [ASREvent(kind="final", text="раз", start_ms=0, end_ms=2000)], None)
        await pipeline._publish("t2", [ASREvent(kind="final", text="два", start_ms=0, end_ms=2000)], None)
        return [event for event in events if event["type"] == "segment"]

    segments = run(scenario())

    assert [segment["segment"]["start_ms"] for segment in segments] == [0, 30_000]
    assert [segment["segment"]["end_ms"] for segment in segments] == [2_000, 32_000]
    ordered = sorted(segments, key=lambda item: item["segment"]["start_ms"])
    assert [item["segment"]["participant_id"] for item in ordered] == ["p-1", "p-2"]


def test_a_placeholder_gives_way_to_the_returning_person() -> None:
    """The track arrives before its tile: the placeholder must become the
    returning participant, not a second tile with the same name."""

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._apply_participant_update(ParticipantUpdate("t1", present=False))

        await pipeline._register_track("t2", "Участник 2")
        await pipeline._apply_participant_update(ParticipantUpdate("t2", name="Адель"))
        return pipeline, participants(events)

    pipeline, people = run(scenario())

    assert list(people) == ["p-1"], f"one person must be one tile, got {list(people)}"
    assert people["p-1"]["display_name"] == "Адель"
    assert people["p-1"]["present"] is True
    assert pipeline._participants["t2"] == "p-1"


def test_a_placeholder_that_spoke_keeps_its_lines() -> None:
    """Once a placeholder produced a line, re-pointing it would lose that line,
    so it keeps its own record under an unambiguous name."""

    async def scenario() -> dict[str, dict[str, Any]]:
        pipeline, events = make_pipeline()
        await pipeline._register_track("t1", "Адель")
        await pipeline._apply_participant_update(ParticipantUpdate("t1", present=False))
        await pipeline._register_track("t2", "Участник 2")
        await pipeline._publish(
            "t2", [ASREvent(kind="final", text="привет", start_ms=0, end_ms=1000)], None
        )
        await pipeline._apply_participant_update(ParticipantUpdate("t2", name="Адель"))
        return participants(events)

    people = run(scenario())

    names = sorted(record["display_name"] for record in people.values())
    assert names == ["Адель", "Адель (2)"], names
