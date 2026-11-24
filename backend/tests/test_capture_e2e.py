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


@pytest.fixture(scope="module")
def server():
    import uvicorn

    os.environ["TRANSCRIBE_DATA_DIR"] = tempfile.mkdtemp(prefix="transcribe-e2e-")
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
    meeting = (
        f"http://127.0.0.1:{PORT}/fake-meeting/?peers=1"
        f"&audio1=/fixtures/{FIXTURE.name}&name1=Анна Соколова"
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

    # A video frame must reach the agent even without a face detector: that is
    # what proves the __grabFace path and the video-track mapping work.
    faces = {part["face_status"] for part in participants}
    assert faces & {"frame", "detected"}, f"no video frame captured: {faces}"

    transcript = " ".join(segment["text"] for segment in payload["segments"]).lower()
    assert payload["segments"], f"no transcript, session state={payload['session']['state']}"
    for expected in ("коллеги", "планёрк", "транскрибац"):
        assert expected in transcript, f"{expected!r} missing in {transcript!r}"

    # Every segment must carry a real speaker, not an "unknown" bucket.
    known = {part["id"] for part in participants}
    assert all(segment["participant_id"] in known for segment in payload["segments"])
