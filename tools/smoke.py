"""Smoke check against a running server: create a session, wait, fetch exports.

Run: .venv\\Scripts\\python.exe tools\\smoke.py [base_url] [speed]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SPEED = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0


def call(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, bytes, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


status, body, _ = call("/api/health")
print("health:", status, body.decode()[:160])

status, body, _ = call(
    "/api/sessions",
    "POST",
    {"meeting_url": "https://jazz.sber.ru/meeting/smoke", "title": "Проверка дыма", "speed": SPEED},
)
print("create:", status)
session_id = json.loads(body)["id"]
print("session:", session_id)

deadline = time.time() + 60
state = "?"
while time.time() < deadline:
    status, body, _ = call(f"/api/sessions/{session_id}")
    payload = json.loads(body)
    state = payload["session"]["state"]
    if state in ("finished", "failed"):
        break
    time.sleep(0.1)

print("final state:", state, "| participants:", len(payload["participants"]), "| segments:", len(payload["segments"]))
for participant in payload["participants"]:
    print("  -", participant["display_name"], participant["role"], participant["face_status"])

for fmt in ("srt", "vtt", "md", "txt", "json"):
    status, body, headers = call(f"/api/sessions/{session_id}/export/{fmt}")
    disposition = headers.get("content-disposition", "")
    print(f"export {fmt}: {status} {len(body)} bytes | {disposition[:70]}")

status, body, _ = call(f"/api/sessions/{session_id}/export/srt")
print("\n--- SRT (first lines) ---")
print(body.decode()[:420])

status, body, _ = call(f"/api/sessions/{session_id}/log")
print("event log lines:", len(body.decode().splitlines()))
