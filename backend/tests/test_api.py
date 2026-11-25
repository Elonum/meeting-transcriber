"""End-to-end tests for the API surface.

The mock pipeline accepts a ``speed`` multiplier so a 90 second meeting runs in
about a second of wall clock, which keeps this suite fast without weakening it:
the same events, the same fan-out, the same exporters.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

# Keep generated sessions inside the repository's gitignored folder: Python
# falls back to the current directory when the system temp is unusable, which
# once put a test session into the source tree.
_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp" / "tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["TRANSCRIBE_DATA_DIR"] = tempfile.mkdtemp(prefix="transcribe-test-", dir=_TMP_ROOT)

from datetime import UTC  # noqa: E402 - the env above must be set first

from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MEETING = "https://jazz.sber.ru/meeting/abc-defg-hij"


@pytest.fixture(scope="module")
def client():
    # The context manager keeps one event loop alive for the whole module, so
    # background pipeline tasks survive between requests.
    with TestClient(app) as test_client:
        yield test_client


def _wait_finished(client: TestClient, session_id: str, timeout: float = 25.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/sessions/{session_id}").json()
        if payload["session"]["state"] in ("finished", "failed"):
            return payload
        time.sleep(0.05)
    raise AssertionError("session did not finish in time")


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["pipelines"]["mock"] is True


def test_rejects_bad_url(client: TestClient) -> None:
    response = client.post("/api/sessions", json={"meeting_url": "не ссылка"})
    assert response.status_code == 422


def test_full_session_lifecycle(client: TestClient) -> None:
    created = client.post(
        "/api/sessions",
        json={
            "meeting_url": MEETING,
            "title": "Планёрка по транскрибации",
            "speed": 30,
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    payload = _wait_finished(client, session_id)
    assert payload["session"]["state"] == "finished"
    assert payload["session"]["error"] is None

    participants = payload["participants"]
    assert len(participants) == 4, participants
    assert participants[0]["role"] == "bot"
    assert any(p["face_status"] == "detected" for p in participants[1:])

    segments = payload["segments"]
    assert len(segments) >= 10
    assert all(s["final"] for s in segments)
    assert all(s["text"].strip() for s in segments)
    assert segments == sorted(segments, key=lambda s: s["start_ms"])
    # Every segment belongs to a known participant: no orphan speech.
    known = {p["id"] for p in participants}
    assert {s["participant_id"] for s in segments} <= known


def test_exports_contain_speakers(client: TestClient) -> None:
    session_id = client.post(
        "/api/sessions", json={"meeting_url": MEETING, "speed": 30}
    ).json()["id"]
    _wait_finished(client, session_id)

    srt = client.get(f"/api/sessions/{session_id}/export/srt")
    assert srt.status_code == 200
    assert "-->" in srt.text and "Анна Соколова" in srt.text

    vtt = client.get(f"/api/sessions/{session_id}/export/vtt").text
    assert vtt.startswith("WEBVTT")

    md = client.get(f"/api/sessions/{session_id}/export/md").text
    assert md.startswith("# ") and "## Транскрипт" in md

    data = client.get(f"/api/sessions/{session_id}/export/json").json()
    assert data["segments"] and data["participants"]

    assert client.get(f"/api/sessions/{session_id}/export/docx").status_code == 400


def test_event_log_is_audit_trail(client: TestClient) -> None:
    session_id = client.post(
        "/api/sessions", json={"meeting_url": MEETING, "speed": 30}
    ).json()["id"]
    _wait_finished(client, session_id)

    lines = [line for line in client.get(f"/api/sessions/{session_id}/log").text.splitlines() if line]
    assert len(lines) > 20
    assert all('"type"' in line for line in lines)


def test_websocket_stream(client: TestClient) -> None:
    session_id = client.post(
        "/api/sessions", json={"meeting_url": MEETING, "speed": 20}
    ).json()["id"]

    with client.websocket_connect(f"/events/{session_id}") as socket:
        hello = socket.receive_json()
        assert hello["type"] == "hello"
        assert hello["session"]["id"] == session_id

        seen = set()
        for _ in range(40):
            event = socket.receive_json()
            seen.add(event["type"])
            if event["type"] == "done":
                break
        assert {"participant", "segment"} <= seen


def test_stop_is_idempotent(client: TestClient) -> None:
    session_id = client.post(
        "/api/sessions", json={"meeting_url": MEETING, "speed": 5}
    ).json()["id"]
    first = client.post(f"/api/sessions/{session_id}/stop")
    second = client.post(f"/api/sessions/{session_id}/stop")
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["state"] in ("finished", "stopping")


def test_unknown_session_is_404(client: TestClient) -> None:
    assert client.get("/api/sessions/nope").status_code == 404
    assert client.post("/api/sessions/nope/stop").status_code == 404


def test_scheduled_session_waits_then_runs(client: TestClient) -> None:
    """The brief asks for "ссылка + время": the agent must wait for the clock."""
    from datetime import datetime, timedelta

    start_at = (datetime.now(UTC) + timedelta(seconds=3)).isoformat()
    created = client.post(
        "/api/sessions",
        json={"meeting_url": MEETING, "start_at": start_at, "speed": 30},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    assert created.json()["start_at_ms"] > 0

    time.sleep(0.4)
    state = client.get(f"/api/sessions/{session_id}").json()["session"]["state"]
    assert state == "scheduled", state
    # Nothing may be transcribed before the start time.
    assert client.get(f"/api/sessions/{session_id}").json()["segments"] == []

    deadline = time.time() + 20
    state = "scheduled"
    while time.time() < deadline:
        state = client.get(f"/api/sessions/{session_id}").json()["session"]["state"]
        if state in ("live", "finished"):
            break
        time.sleep(0.2)
    assert state in ("live", "finished"), state

    payload = _wait_finished(client, session_id)
    assert payload["segments"], "scheduled session produced no transcript"


def test_past_start_at_is_rejected(client: TestClient) -> None:
    from datetime import datetime, timedelta

    start_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    response = client.post("/api/sessions", json={"meeting_url": MEETING, "start_at": start_at})
    assert response.status_code == 422
    assert "прошло" in response.text


def test_scheduled_session_can_be_cancelled(client: TestClient) -> None:
    from datetime import datetime, timedelta

    start_at = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    created = client.post("/api/sessions", json={"meeting_url": MEETING, "start_at": start_at})
    session_id = created.json()["id"]
    time.sleep(0.3)
    assert client.post(f"/api/sessions/{session_id}/stop").status_code == 200
    time.sleep(0.3)
    state = client.get(f"/api/sessions/{session_id}").json()["session"]["state"]
    assert state == "finished", state
