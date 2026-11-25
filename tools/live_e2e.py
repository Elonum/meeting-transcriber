"""End-to-end against the real SaluteJazz: host speaks, the product transcribes.

Starts a host (tools/host_room.py) with a synthesized Russian WAV as its
microphone, waits for the room, starts a ``jazz`` session through the API, and
prints the transcript that comes back.

    .venv\\Scripts\\python.exe tools\\live_e2e.py --seconds 150

Requires the API on http://127.0.0.1:8000 with the ``jazz`` pipeline available,
and it launches browsers, so it must run outside the file sandbox.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"
STATE = ROOT / ".artifacts" / "host-room.json"


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return json.loads(response.read())


def post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=150)
    parser.add_argument("--host-minutes", type=float, default=4)
    parser.add_argument("--keep-host", action="store_true")
    args = parser.parse_args()

    STATE.parent.mkdir(exist_ok=True)
    if STATE.exists():
        STATE.unlink()

    host = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "host_room.py"), "--minutes", str(args.host_minutes)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"host started (pid {host.pid})")

    room_url = None
    deadline = time.time() + 75
    while time.time() < deadline:
        if STATE.exists() and STATE.stat().st_size > 0:
            room_url = json.loads(STATE.read_text(encoding="utf-8"))["url"]
            break
        time.sleep(2)
    if not room_url:
        print("host did not create a room")
        host.terminate()
        return 1
    print("room:", room_url)

    session = post(
        "/api/sessions",
        {
            "meeting_url": room_url,
            "title": "Живая встреча SaluteJazz",
            "pipeline": "jazz",
            "headless": True,
        },
    )
    print("session:", session["id"])

    deadline = time.time() + args.seconds
    payload: dict = {}
    seen = -1
    try:
        while time.time() < deadline:
            payload = get(f"/api/sessions/{session['id']}")
            segments = len(payload["segments"])
            if segments != seen:
                print(
                    f"  state={payload['session']['state']} "
                    f"participants={len(payload['participants'])} segments={segments}",
                    flush=True,
                )
                seen = segments
            if segments >= 3 or payload["session"]["state"] in ("finished", "failed"):
                break
            time.sleep(3)
    finally:
        try:
            post(f"/api/sessions/{session['id']}/stop", {})
        except Exception:  # noqa: BLE001
            pass
        if not args.keep_host:
            host.terminate()
            try:
                host.wait(timeout=10)
            except Exception:  # noqa: BLE001
                host.kill()

    (ROOT / ".artifacts" / "live-e2e.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    segments = payload.get("segments", [])
    print(f"\nfinal: state={payload.get('session', {}).get('state')} segments={len(segments)}")
    for part in payload.get("participants", []):
        print("  participant:", part["display_name"], part["face_status"])
    for segment in segments:
        print(f"  [{segment['start_ms']}ms] {segment['participant_id']}: {segment['text']}")

    # Recording and the all-in-one archive are part of the deliverable.
    detail = get(f"/api/sessions/{session['id']}")
    audio = detail["session"].get("audio") or []
    print("\nrecorded audio:", [(item["file"], f"{item['duration_s']}s") for item in audio])
    bundle_ok = False
    if segments:
        with urllib.request.urlopen(BASE + f"/api/sessions/{session['id']}/export/zip", timeout=120) as response:
            archive_bytes = response.read()
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = archive.namelist()
        bundle_ok = "transcript.srt" in names and (not audio or any(n.startswith("audio/") for n in names))
        print("bundle:", len(archive_bytes), "bytes,", len(names), "files, audio included:", bundle_ok)

    text = " ".join(segment["text"] for segment in segments).lower()
    ok = any(word in text for word in ("коллеги", "планёрк", "транскрибац", "дмитрий"))
    print("\nRESULT:", "transcript captured" if ok else "no expected words", "| audio:", bool(audio), "| zip:", bundle_ok)
    return 0 if ok and bundle_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
