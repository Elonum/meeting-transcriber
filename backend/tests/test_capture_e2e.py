"""End-to-end: browser agent → PCM → VAD → Whisper → transcript.

This is the test that proves the real capture path works. It needs a browser, a
downloaded model and a synthesized speech fixture, so it is opt-in:

    # once: generate fixtures and download the model
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\\make_speech.ps1 `
        -Text "Коллеги, начинаем планёрку." -Out .artifacts\\speech\\ru-1.wav

    # then:
    $env:TRANSCRIBE_E2E = "1"
    .venv\\Scripts\\python.exe -m pytest backend\\tests\\test_capture_e2e.py -q -s

The meeting is served by the app itself at ``/fake-meeting``: participants talk
over real RTCPeerConnections inside the page, so the hook, the audio resampling,
the packetisation and the ASR pipeline are all exercised.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

E2E = os.environ.get("TRANSCRIBE_E2E") == "1"
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / ".artifacts" / "speech" / "ru-1.wav"
FACE = ROOT / ".artifacts" / "face-test.jpg"
PORT = 8799

pytestmark = pytest.mark.skipif(
    not E2E or not FIXTURE.is_file(),
    reason="set TRANSCRIBE_E2E=1 and generate .artifacts/speech/ru-1.wav first",
)


def _post(path: str, payload: dict) -> dict:
    import json

    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _get(path: str) -> dict:
    import json

    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=30) as response:
        return json.loads(response.read())


# Keep generated sessions inside the repository's gitignored folder: Python
# falls back to the current directory when the system temp is unusable, which
# once put a test session into the source tree.
_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp" / "tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["TRANSCRIBE_DATA_DIR"] = tempfile.mkdtemp(prefix="transcribe-e2e-", dir=_TMP_ROOT)


@pytest.fixture(scope="module")
def server():
    import uvicorn

    from app.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            _get("/api/health")
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    else:
        pytest.fail("server did not start")

    yield
    server.should_exit = True
    thread.join(timeout=5)


def test_agent_transcribes_a_real_meeting(server) -> None:
    face_param = ""
    if FACE.is_file():
        # Serve the photo from the fixture directory the app already exposes.
        target = FIXTURE.parent / FACE.name
        if not target.is_file():
            target.write_bytes(FACE.read_bytes())
        face_param = f"&face1=/fixtures/{target.name}"
    meeting = (
        f"http://127.0.0.1:{PORT}/fake-meeting/?peers=1"
        f"&audio1=/fixtures/{FIXTURE.name}&name1=Анна Соколова{face_param}"
    )
    created = _post(
        "/api/sessions",
        {
            "meeting_url": meeting,
            "title": "E2E захват",
            "pipeline": "jazz",
            "headless": True,
            "model": "large-v3-turbo",
        },
    )
    session_id = created["id"]

    deadline = time.time() + 180
    payload: dict = {}
    last_count = 0
    last_change = time.time()
    while time.time() < deadline:
        payload = _get(f"/api/sessions/{session_id}")
        if payload["session"]["state"] in ("finished", "failed"):
            break
        if len(payload["segments"]) != last_count:
            last_count = len(payload["segments"])
            last_change = time.time()
        elif last_count and time.time() - last_change > 5:
            # Transcript settled: the fixture has been fully spoken.
            _post(f"/api/sessions/{session_id}/stop", {})
        time.sleep(1.5)

    artifact = ROOT / ".artifacts" / "e2e-transcript.txt"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text(
        "\n".join(f"[{s['start_ms']}-{s['end_ms']}] {s['participant_id']}: {s['text']}" for s in payload["segments"]),
        encoding="utf-8",
    )

    participants = payload["participants"]
    assert any(part["display_name"] == "Анна Соколова" for part in participants), participants

    # A video frame must reach the agent; with a real photo in the fake meeting
    # the YuNet detector must also report a face with a thumbnail.
    faces = {part["face_status"] for part in participants}
    if face_param:
        assert "detected" in faces, f"face detector did not fire: {faces}"
        assert any(part["face_confidence"] for part in participants)
    else:
        assert faces & {"frame", "detected"}, f"no video frame captured: {faces}"

    transcript = " ".join(segment["text"] for segment in payload["segments"]).lower()
    assert payload["segments"], f"no transcript, session state={payload['session']['state']}"
    for expected in ("коллеги", "планёрк", "транскрибац"):
        assert expected in transcript, f"{expected!r} missing in {transcript!r}"

    # Every segment must carry a real speaker, not an "unknown" bucket.
    known = {part["id"] for part in participants}
    assert all(segment["participant_id"] in known for segment in payload["segments"])


def test_two_participants_keep_their_own_words(server) -> None:
    """The core promise: two people talk at once, each keeps their own words."""
    second = FIXTURE.parent / "ru-2.wav"
    if not second.is_file():
        pytest.skip("second speech fixture missing")

    meeting = (
        f"http://127.0.0.1:{PORT}/fake-meeting/?peers=2"
        f"&audio1=/fixtures/{FIXTURE.name}&name1=Анна Соколова"
        f"&audio2=/fixtures/{second.name}&name2=Дмитрий Лебедев"
    )
    created = _post(
        "/api/sessions",
        {
            "meeting_url": meeting,
            "title": "E2E два спикера",
            "pipeline": "jazz",
            "headless": True,
            "model": "large-v3-turbo",
        },
    )
    session_id = created["id"]

    deadline = time.time() + 240
    payload: dict = {}
    last_count = 0
    last_change = time.time()
    while time.time() < deadline:
        payload = _get(f"/api/sessions/{session_id}")
        if payload["session"]["state"] in ("finished", "failed"):
            break
        segments = payload["segments"]
        if len(segments) != last_count:
            last_count = len(segments)
            last_change = time.time()
        elif last_count >= 2 and time.time() - last_change > 8:
            _post(f"/api/sessions/{session_id}/stop", {})
        time.sleep(1.5)

    names = {part["id"]: part["display_name"] for part in payload["participants"]}
    by_name: dict[str, list[str]] = {}
    for segment in payload["segments"]:
        by_name.setdefault(names.get(segment["participant_id"], "?"), []).append(segment["text"])

    assert set(by_name) >= {"Анна Соколова", "Дмитрий Лебедев"}, f"speakers: {by_name}"

    anna = " ".join(by_name["Анна Соколова"]).lower()
    dmitry = " ".join(by_name["Дмитрий Лебедев"]).lower()

    # Each phrase belongs to the track it was spoken on — no cross-talk.
    assert "планёрк" in anna, anna
    assert "пятниц" in dmitry, dmitry
    assert "пятниц" not in anna, f"Дмитрий's words leaked into Анна: {anna}"
    assert "планёрк" not in dmitry, f"Анна's words leaked into Дмитрий: {dmitry}"
