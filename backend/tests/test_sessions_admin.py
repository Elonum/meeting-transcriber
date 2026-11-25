"""History management: search, deletion, audio files and the ZIP bundle.

These exercise the API the way the UI does, against real files on disk, so a
regression in path handling or cleanup shows up here rather than in production.
"""

from __future__ import annotations

import io
import time
import zipfile

import pytest
from app.main import DATA_DIR, app  # noqa: F401 - importing sets the data dir
from app.recording import TrackRecorder
from fastapi.testclient import TestClient

MEETING = "https://salutejazz.ru/calls/abc123?psw=ZZZZZZZZ"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _finished(client: TestClient, speed: int = 40) -> str:
    session_id = client.post(
        "/api/sessions", json={"meeting_url": MEETING, "title": "Поиск и удаление", "speed": speed}
    ).json()["id"]
    deadline = time.time() + 40
    while time.time() < deadline:
        payload = client.get(f"/api/sessions/{session_id}").json()
        if payload["session"]["state"] in ("finished", "failed"):
            return session_id
        time.sleep(0.05)
    raise AssertionError("session did not finish")


def _session_dir(session_id: str):
    return DATA_DIR / session_id


def test_search_by_transcript_text(client: TestClient) -> None:
    session_id = _finished(client)

    body = client.get("/api/sessions", params={"q": "транскрибацию"}).json()
    ids = [item["id"] for item in body["sessions"]]
    assert session_id in ids, "the transcript word should match its own session"
    match = next(item for item in body["sessions"] if item["id"] == session_id)
    assert "транскрибацию" in (match.get("snippet") or "").lower()
    assert body["query"] == "транскрибацию"


def test_search_by_participant_and_title(client: TestClient) -> None:
    session_id = _finished(client)
    by_name = client.get("/api/sessions", params={"q": "Дмитрий"}).json()["sessions"]
    assert session_id in [item["id"] for item in by_name]

    by_title = client.get("/api/sessions", params={"q": "удаление"}).json()["sessions"]
    assert session_id in [item["id"] for item in by_title]


def test_search_without_matches(client: TestClient) -> None:
    body = client.get("/api/sessions", params={"q": "нет-такого-слова-12345"}).json()
    assert body["sessions"] == []
    assert body["total"] == 0


def test_empty_query_returns_everything(client: TestClient) -> None:
    all_sessions = client.get("/api/sessions").json()
    blank = client.get("/api/sessions", params={"q": "   "}).json()
    assert len(blank["sessions"]) == len(all_sessions["sessions"])


def test_delete_removes_session_and_files(client: TestClient) -> None:
    session_id = _finished(client)
    directory = _session_dir(session_id)
    assert directory.is_dir()

    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.get(f"/api/sessions/{session_id}").status_code == 404
    assert not directory.exists(), "session folder must be gone"
    # Deleting twice is a 404, not a crash.
    assert client.delete(f"/api/sessions/{session_id}").status_code == 404


def test_delete_stops_a_running_session(client: TestClient) -> None:
    session_id = client.post(
        "/api/sessions", json={"meeting_url": MEETING, "title": "Идёт", "speed": 0.5}
    ).json()["id"]
    time.sleep(0.3)
    assert client.get(f"/api/sessions/{session_id}").json()["session"]["state"] == "live"

    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_malformed_id_is_not_found(client: TestClient) -> None:
    for bad in ("ZZZZZZ", "../../etc", "abc", "0" * 40):
        assert client.get(f"/api/sessions/{bad}").status_code == 404


def test_audio_is_listed_and_served(client: TestClient) -> None:
    session_id = _finished(client)
    audio_dir = _session_dir(session_id) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    recorder = TrackRecorder(audio_dir / "p-1.wav")
    recorder.write(b"\x00\x01" * 16_000)  # one second of 16 kHz PCM16
    recorder.close()

    payload = client.get(f"/api/sessions/{session_id}").json()
    audio = payload["session"]["audio"]
    assert len(audio) == 1
    assert audio[0]["participant_id"] == "p-1"
    assert audio[0]["duration_s"] == pytest.approx(1.0, abs=0.1)

    served = client.get(f"/api/sessions/{session_id}/audio/p-1.wav")
    assert served.status_code == 200
    assert served.headers["content-type"] == "audio/wav"
    assert len(served.content) > 32_000


def test_audio_rejects_unknown_and_traversal(client: TestClient) -> None:
    session_id = _finished(client)
    assert client.get(f"/api/sessions/{session_id}/audio/missing.wav").status_code == 404
    assert client.get(f"/api/sessions/{session_id}/audio/..%2Fmeta.json").status_code == 404
    assert client.get(f"/api/sessions/{session_id}/audio/passwd").status_code == 404


def test_zip_bundle_contains_transcript_log_and_audio(client: TestClient) -> None:
    session_id = _finished(client)
    audio_dir = _session_dir(session_id) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    recorder = TrackRecorder(audio_dir / "p-1.wav")
    recorder.write(b"\x00\x02" * 8_000)
    recorder.close()

    response = client.get(f"/api/sessions/{session_id}/export/zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {"transcript.srt", "transcript.vtt", "transcript.md", "transcript.txt", "transcript.json"} <= names
        assert "README.txt" in names
        assert "events.jsonl" in names
        assert "audio/p-1.wav" in names
        assert "Анна Соколова" in archive.read("transcript.srt").decode("utf-8")
        assert "p-1.wav" in archive.read("README.txt").decode("utf-8")

    # The temporary archive is removed once the response is served.
    assert not (_session_dir(session_id) / "meeting.zip").exists()


def test_zip_without_audio_still_works(client: TestClient) -> None:
    session_id = _finished(client)
    response = client.get(f"/api/sessions/{session_id}/export/zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "audio/p-1.wav" not in archive.namelist()
        assert "transcript.json" in archive.namelist()


def test_unknown_export_format(client: TestClient) -> None:
    session_id = _finished(client)
    assert client.get(f"/api/sessions/{session_id}/export/docx").status_code == 400
