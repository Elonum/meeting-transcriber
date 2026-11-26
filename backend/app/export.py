"""Exporters: one transcript, four formats.

The transcript is always taken from *final* segments only, so interim
revisions never leak into a downloaded file.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

FORMATS = ("srt", "vtt", "txt", "md", "json")


def _ts(ms: int, sep: str) -> str:
    ms = max(0, int(ms))
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{millis:03d}"


def _names(participants: Iterable[dict[str, Any]]) -> dict[str, str]:
    names = {part["id"]: part["display_name"] for part in participants}
    names.setdefault("unknown", "Неизвестный спикер")
    return names


def _ordered(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (s for s in segments if s.get("final")),
        key=lambda s: (s["start_ms"], s["id"]),
    )


def to_srt(segments: Iterable[dict[str, Any]], names: dict[str, str]) -> str:
    blocks: list[str] = []
    for index, seg in enumerate(_ordered(segments), start=1):
        speaker = names.get(seg["participant_id"], "Спикер")
        blocks.append(
            f"{index}\n"
            f"{_ts(seg['start_ms'], ',')} --> {_ts(seg['end_ms'], ',')}\n"
            f"{speaker}: {seg['text']}\n"
        )
    return "\n".join(blocks)


def to_vtt(segments: Iterable[dict[str, Any]], names: dict[str, str]) -> str:
    blocks: list[str] = ["WEBVTT\n"]
    for seg in _ordered(segments):
        speaker = names.get(seg["participant_id"], "Спикер")
        blocks.append(
            f"{_ts(seg['start_ms'], '.')} --> {_ts(seg['end_ms'], '.')}\n"
            f"<v {speaker}>{seg['text']}\n"
        )
    return "\n".join(blocks)


def to_txt(segments: Iterable[dict[str, Any]], names: dict[str, str]) -> str:
    lines: list[str] = []
    for seg in _ordered(segments):
        speaker = names.get(seg["participant_id"], "Спикер")
        lines.append(f"[{_ts(seg['start_ms'], '.')}] {speaker}: {seg['text']}")
    return "\n".join(lines) + ("\n" if lines else "")


def local_start(session: dict[str, Any]) -> datetime:
    """Start time in the machine's timezone.

    The transcript is read by the people who were in the meeting, so a UTC stamp
    is a small but constant annoyance; ``astimezone()`` keeps the offset visible
    for anyone who opens the file elsewhere.
    """
    moment = datetime.fromtimestamp(session["started_at_ms"] / 1000, tz=UTC)
    return moment.astimezone()


def to_md(
    session: dict[str, Any],
    segments: Iterable[dict[str, Any]],
    names: dict[str, str],
    participants: Iterable[dict[str, Any]],
) -> str:
    started = local_start(session)
    people = ", ".join(
        part["display_name"] + (" (бот)" if part["role"] == "bot" else "")
        for part in participants
    )
    lines = [
        f"# {session['title']}",
        "",
        f"- **Ссылка:** {session['meeting_url']}",
        f"- **Начало:** {started:%Y-%m-%d %H:%M %z}",
        f"- **Длительность:** {_duration(segments)}",
        f"- **Пайплайн:** `{session['pipeline']}`",
        f"- **Участники:** {people or '—'}",
        "",
        "## Транскрипт",
        "",
    ]
    for seg in _ordered(segments):
        speaker = names.get(seg["participant_id"], "Спикер")
        lines.append(f"**{_ts(seg['start_ms'], '.')[:8]} · {speaker}**  \n{seg['text']}")
        lines.append("")
    return "\n".join(lines)


def _duration(segments: Iterable[dict[str, Any]]) -> str:
    ordered = _ordered(segments)
    if not ordered:
        return "—"
    total = ordered[-1]["end_ms"]
    minutes, seconds = divmod(int(total // 1000), 60)
    return f"{minutes} мин {seconds} с"


def to_json(
    session: dict[str, Any],
    segments: Iterable[dict[str, Any]],
    participants: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "session": session,
        "participants": [
            {
                "id": part["id"],
                "display_name": part["display_name"],
                "role": part.get("role", "human"),
                "face_status": part.get("face_status", "unknown"),
                "face_confidence": part.get("face_confidence"),
                "has_video": part.get("has_video", False),
                "present": part.get("present", True),
            }
            for part in participants
        ],
        "segments": [
            {
                "id": seg["id"],
                "participant_id": seg["participant_id"],
                "start_ms": seg["start_ms"],
                "end_ms": seg["end_ms"],
                "text": seg["text"],
                "confidence": seg.get("confidence"),
                "language": seg.get("language"),
            }
            for seg in _ordered(segments)
        ],
    }


CONTENT_TYPES = {
    "srt": "application/x-subrip; charset=utf-8",
    "vtt": "text/vtt; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json; charset=utf-8",
}


def render(
    fmt: str,
    session: dict[str, Any],
    segments: list[dict[str, Any]],
    participants: list[dict[str, Any]],
) -> str:
    names = _names(participants)
    if fmt == "srt":
        return to_srt(segments, names)
    if fmt == "vtt":
        return to_vtt(segments, names)
    if fmt == "txt":
        return to_txt(segments, names)
    if fmt == "md":
        return to_md(session, segments, names, participants)
    if fmt == "json":
        import json

        return json.dumps(to_json(session, segments, participants), ensure_ascii=False, indent=2)
    raise ValueError(f"unknown format: {fmt}")
