"""Exporters: readable protocol, local clock.

The transcript is read by the people who were in the meeting, so a UTC stamp is a
small but constant annoyance — and an easy regression to miss.
"""

from __future__ import annotations

from datetime import datetime

from app import export as exporters

STARTED_AT_MS = 1_764_000_000_000
SEGMENTS = [
    {"id": "s1", "participant_id": "p-1", "start_ms": 0, "end_ms": 2000, "text": "Коллеги, начинаем.", "final": True},
    {"id": "s2", "participant_id": "p-2", "start_ms": 2500, "end_ms": 4000, "text": "Да, я готов.", "final": True},
]
PARTICIPANTS = [
    {"id": "p-1", "display_name": "Роман", "role": "human", "face_status": "detected"},
    {"id": "p-agent", "display_name": "Ассистент (бот)", "role": "bot", "face_status": "none"},
]


def session() -> dict:
    return {
        "id": "abc123",
        "title": "Планёрка",
        "meeting_url": "https://salutejazz.ru/calls/abc123",
        "started_at_ms": STARTED_AT_MS,
        "pipeline": "jazz",
    }


def test_local_start_carries_an_offset() -> None:
    moment = exporters.local_start(session())

    assert moment.tzinfo is not None
    assert moment.utcoffset() is not None
    # The same instant, expressed in the machine's zone.
    expected = datetime.fromtimestamp(STARTED_AT_MS / 1000).astimezone()
    assert moment.utcoffset() == expected.utcoffset()


def test_markdown_uses_local_time_not_utc() -> None:
    text = exporters.render("md", session(), SEGMENTS, PARTICIPANTS)
    offset = exporters.local_start(session()).strftime("%z")

    assert "Начало:" in text
    assert "UTC" not in text
    assert offset and offset in text


def test_every_format_renders_a_speaker() -> None:
    for fmt in exporters.FORMATS:
        text = exporters.render(fmt, session(), SEGMENTS, PARTICIPANTS)
        assert "Роман" in text, fmt
        assert "Коллеги, начинаем." in text, fmt


def test_segments_keep_speech_order() -> None:
    shuffled = [SEGMENTS[1], SEGMENTS[0]]
    text = exporters.render("txt", session(), shuffled, PARTICIPANTS)

    assert text.index("Коллеги") < text.index("я готов")
