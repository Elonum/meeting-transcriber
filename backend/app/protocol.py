"""Wire protocol shared by the backend, the web UI and the export layer.

Everything the browser sees is a single JSON object per WebSocket message.
The same objects are appended to ``events.jsonl`` on disk, which makes the log
the single source of truth: replaying the file rebuilds the exact session state
(see :func:`dsh_replay` logic in ``session.py``).

Server -> client events
-----------------------
hello       Snapshot sent once on connect: session + participants + final segments.
session     Session state changed (scheduled / connecting / live / finished / failed).
participant A participant was created or updated (speaking flag, face status, name).
speech      Short "is speaking right now" toggle, used for the UI highlight only.
segment     A transcript segment: ``final=false`` is an interim (grey) revision,
            ``final=true`` replaces every interim of the same ``id``.
notice      Human-readable system line shown in the transcript feed.
done        The pipeline finished; the session is complete.

Client -> server
----------------
The socket is read-only for the browser. Actions (start / stop) go over REST so
that state changes are always transactional and auditable.
"""

from __future__ import annotations

import time
from typing import Any

# Session lifecycle -----------------------------------------------------------

SCHEDULED = "scheduled"
CONNECTING = "connecting"
LIVE = "live"
STOPPING = "stopping"
FINISHED = "finished"
FAILED = "failed"

TERMINAL_STATES = frozenset({FINISHED, FAILED})


def ev_session(state: str, at_ms: int, **extra: Any) -> dict[str, Any]:
    return {"type": "session", "at_ms": at_ms, "state": state, **extra}


def ev_participant(at_ms: int, participant: dict[str, Any]) -> dict[str, Any]:
    return {"type": "participant", "at_ms": at_ms, "participant": participant}


def ev_speech(at_ms: int, participant_id: str, speaking: bool) -> dict[str, Any]:
    return {
        "type": "speech",
        "at_ms": at_ms,
        "participant_id": participant_id,
        "speaking": speaking,
    }


def ev_segment(at_ms: int, segment: dict[str, Any]) -> dict[str, Any]:
    return {"type": "segment", "at_ms": at_ms, "segment": segment}


def ev_face(
    at_ms: int,
    participant_id: str,
    status: str,
    confidence: float | None = None,
    thumbnail: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "face",
        "at_ms": at_ms,
        "participant_id": participant_id,
        "status": status,
    }
    if confidence is not None:
        payload["confidence"] = round(confidence, 3)
    if thumbnail is not None:
        payload["thumbnail"] = thumbnail
    return payload


def ev_notice(at_ms: int, message: str, level: str = "info") -> dict[str, Any]:
    return {"type": "notice", "at_ms": at_ms, "level": level, "message": message}


def ev_done(at_ms: int, reason: str) -> dict[str, Any]:
    return {"type": "done", "at_ms": at_ms, "reason": reason}


# Participants ----------------------------------------------------------------

PALETTE = ["#6c8cff", "#3fc8a8", "#f0a84c", "#e3688f", "#9b7bff", "#4bb6e8"]


def new_participant(
    participant_id: str,
    display_name: str,
    *,
    role: str = "human",
    has_video: bool = True,
    color_index: int = 0,
) -> dict[str, Any]:
    return {
        "id": participant_id,
        "display_name": display_name,
        "role": role,
        "has_video": has_video,
        "color": PALETTE[color_index % len(PALETTE)],
        "speaking": False,
        "present": True,
        "face_status": "unknown",
        "face_confidence": None,
        "thumbnail": None,
        "joined_at_ms": 0,
    }


def now_ms() -> int:
    return int(time.time() * 1000)
