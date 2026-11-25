"""Poll one live session and print its progress.

    .venv\\Scripts\\python.exe tools\\live_check.py <session_id> [--seconds 120]
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    parser.add_argument("--seconds", type=int, default=120)
    args = parser.parse_args()

    deadline = time.time() + args.seconds
    payload: dict = {}
    seen = -1
    while time.time() < deadline:
        payload = get(f"/api/sessions/{args.session_id}")
        state = payload["session"]["state"]
        segments = len(payload["segments"])
        participants = len(payload["participants"])
        if segments != seen or state in ("finished", "failed"):
            print(f"state={state} participants={participants} segments={segments}", flush=True)
            seen = segments
        if state in ("finished", "failed") or segments >= 3:
            break
        time.sleep(3)

    (ROOT / ".artifacts" / "live-session.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("final state:", payload.get("session", {}).get("state"))
    for part in payload.get("participants", []):
        print("  participant:", part["display_name"], part["face_status"])
    for segment in payload.get("segments", [])[:8]:
        print(f"  [{segment['start_ms']}ms] {segment['participant_id']}: {segment['text'][:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
