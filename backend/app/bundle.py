"""One archive with everything a meeting produced.

The brief asked for a file with "everything" on the frontend; a ZIP is the honest
answer: transcript in five formats, the raw event log and the recorded audio, plus
a manifest that maps participant ids to names.

Text is deflated; WAV is stored — PCM does not compress, and spending CPU on it
would only make the download slower to start.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from . import export as exporters
from .export import local_start
from .recording import scan_audio_dir
from .session import Session

BUNDLE_NAME = "meeting.zip"
TEXT_FORMATS = ("srt", "vtt", "md", "txt", "json")


def _manifest(session: Session) -> str:
    started = local_start(session.as_dict())
    audio = scan_audio_dir(session.dir / "audio")
    lines = [
        f"Встреча: {session.title}",
        f"Ссылка: {session.meeting_url}",
        f"Начало: {started:%Y-%m-%d %H:%M %z}",
        f"Пайплайн: {session.pipeline.name}",
        f"Реплик: {len(session.final_segments())}",
        "",
        "Участники:",
    ]
    for part in session.participants.values():
        role = "бот" if part.get("role") == "bot" else "участник"
        lines.append(f"  {part['display_name']} ({role}) — id {part['id']}")
    if audio:
        lines.extend(["", "Записи:"])
        lines.extend(f"  audio/{item.file} — {item.duration_s} с" for item in audio)
    else:
        lines.extend(["", "Аудио не записывалось."])
    lines.extend(
        [
            "",
            "Файлы:",
            "  transcript.srt  — субтитры",
            "  transcript.vtt  — веб-субтитры",
            "  transcript.md   — протокол",
            "  transcript.txt  — плоский текст",
            "  transcript.json — данные для обработки",
            "  events.jsonl    — журнал событий сессии",
        ]
    )
    return "\n".join(lines) + "\n"


def build_bundle(session: Session) -> Path:
    """Write ``meeting.zip`` into the session directory and return its path."""
    session.dir.mkdir(parents=True, exist_ok=True)
    target = session.dir / BUNDLE_NAME
    participants = list(session.participants.values())
    metadata = session.as_dict()

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for fmt in TEXT_FORMATS:
            archive.writestr(f"transcript.{fmt}", exporters.render(fmt, metadata, session.final_segments(), participants))

        archive.writestr("README.txt", _manifest(session))

        events = session.dir / "events.jsonl"
        if events.is_file():
            archive.write(events, "events.jsonl")

        audio_dir = session.dir / "audio"
        if audio_dir.is_dir():
            for info in scan_audio_dir(audio_dir):
                path = audio_dir / info.file
                if path.is_file():
                    # PCM does not compress: store it and skip the CPU burn.
                    archive.write(path, f"audio/{info.file}", compress_type=zipfile.ZIP_STORED)

    return target
