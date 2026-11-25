"""Acceptance run: one command that proves the brief is covered.

Walks the objective item by item against a running server and prints a table:
link/time form, mock session, live agent over the meeting double, faces,
per-speaker attribution, exports, event log, WebSocket stream.

    # mock + API only (fast, no browser needed)
    .venv\\Scripts\\python.exe tools\\acceptance.py --base http://127.0.0.1:8000

    # everything, including the browser agent (server must be able to launch one)
    .venv\\Scripts\\python.exe tools\\acceptance.py --full

Exit code is 0 only when every check passes. A JSON report is written to
.artifacts/acceptance-<timestamp>.json so the result can be attached to a review.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[dict] = []
MEETING = "https://salutejazz.ru/acceptance"


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def call(base: str, path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, bytes, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def wait_finished(base: str, session_id: str, timeout: float = 180) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        _status, body, _headers = call(base, f"/api/sessions/{session_id}")
        payload = json.loads(body)
        if payload["session"]["state"] in ("finished", "failed"):
            return payload
        time.sleep(0.2)
    return payload


async def check_ws(base: str) -> tuple[bool, str]:
    """Start a session and watch it stream: the socket is the demo's beating heart."""
    import websockets

    _status, body, _headers = await asyncio.to_thread(
        call,
        base,
        "/api/sessions",
        "POST",
        {"meeting_url": MEETING, "title": "Приёмка WS", "speed": 5, "pipeline": "mock"},
    )
    session_id = json.loads(body).get("id", "")
    if not session_id:
        return False, "session was not created"

    url = base.replace("http://", "ws://").replace("https://", "wss://") + f"/events/{session_id}"
    try:
        async with websockets.connect(url, open_timeout=10) as socket:
            hello = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            if hello.get("type") != "hello":
                return False, f"first message was {hello.get('type')}"

            kinds = set()
            deadline = time.time() + 25
            while time.time() < deadline:
                try:
                    event = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
                except asyncio.TimeoutError:
                    break
                kinds.add(event.get("type"))
                if event.get("type") == "segment":
                    await asyncio.to_thread(call, base, f"/api/sessions/{session_id}/stop", "POST")
                    return True, f"events: {sorted(kinds)}"
            await asyncio.to_thread(call, base, f"/api/sessions/{session_id}/stop", "POST")
            return False, f"no segment event, saw {sorted(kinds)}"
    except Exception as error:  # noqa: BLE001
        return False, str(error)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--full", action="store_true", help="also run the browser agent checks")
    parser.add_argument("--meeting", default=None, help="real Jazz URL instead of the meeting double")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    # 1. service is up and both pipelines are advertised -----------------------
    status, body, _ = call(base, "/api/health")
    health = json.loads(body) if status == 200 else {}
    record("сервис отвечает", status == 200 and health.get("status") == "ok", str(health.get("status")))
    record("пайплайн mock доступен", bool(health.get("pipelines", {}).get("mock")))
    record("пайплайн jazz доступен (ASR установлен)", bool(health.get("pipelines", {}).get("jazz")))

    # 2. UI is served ---------------------------------------------------------
    status, body, _ = call(base, "/")
    record("веб-интерфейс отдаётся", status == 200 and b'id="root"' in body, f"{len(body)} bytes")

    # 3. mock session: form (link) → participants, faces, transcript ----------
    status, body, _ = call(
        base,
        "/api/sessions",
        "POST",
        {"meeting_url": MEETING, "title": "Приёмка", "speed": 25, "pipeline": "mock"},
    )
    created = json.loads(body) if status == 201 else {}
    session_id = created.get("id", "")
    record("сессия создаётся по ссылке", status == 201 and bool(session_id), session_id)

    payload = wait_finished(base, session_id, timeout=120)
    participants = payload.get("participants", [])
    segments = payload.get("segments", [])
    record("участники появились", len(participants) >= 4, f"{len(participants)} участников")
    record("лица распознаны", len([p for p in participants if p["face_status"] == "detected"]) >= 3)
    record("транскрипт пишется", len(segments) >= 10, f"{len(segments)} реплик")
    known = {p["id"] for p in participants}
    record(
        "каждая реплика привязана к участнику",
        all(s["participant_id"] in known for s in segments) and bool(segments),
    )
    record(
        "таймкоды заполнены",
        all(s["end_ms"] > s["start_ms"] >= 0 for s in segments),
    )

    # 4. exports --------------------------------------------------------------
    expectations = {
        "srt": (b"-->", b"\xd0"),
        "vtt": (b"WEBVTT", b"-->"),
        "md": (b"# ", b"## "),
        "txt": (b"[", b": "),
        "json": (b'"segments"', b'"participants"'),
    }
    for fmt, needles in expectations.items():
        status, body, headers = call(base, f"/api/sessions/{session_id}/export/{fmt}")
        ok = status == 200 and all(needle in body for needle in needles) and len(body) > 100
        record(f"экспорт {fmt.upper()}", ok, f"{len(body)} bytes, disposition={'yes' if 'content-disposition' in headers else 'no'}")

    # 5. event log (audit trail) ---------------------------------------------
    status, body, _ = call(base, f"/api/sessions/{session_id}/log")
    lines = [line for line in body.decode("utf-8", "replace").splitlines() if line.strip()]
    record("журнал событий ведётся", status == 200 and len(lines) > 20, f"{len(lines)} событий")

    # 6. websocket stream -----------------------------------------------------
    ok, detail = asyncio.run(check_ws(base))
    record("WebSocket отдаёт события в реальном времени", ok, detail)

    # 7. scheduling: link + time ---------------------------------------------
    start_at = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    status, body, _ = call(
        base, "/api/sessions", "POST", {"meeting_url": MEETING, "start_at": start_at, "pipeline": "mock"}
    )
    scheduled = json.loads(body) if status == 201 else {}
    time.sleep(0.4)
    _s, snap, _h = call(base, f"/api/sessions/{scheduled.get('id', '')}")
    state = json.loads(snap)["session"]["state"] if _s == 200 else "?"
    record("время старта принимается", status == 201 and scheduled.get("start_at_ms", 0) > 0, state)
    _s, _b, _h = call(base, f"/api/sessions/{scheduled.get('id', '')}/stop", "POST")
    time.sleep(0.4)
    _s, snap, _h = call(base, f"/api/sessions/{scheduled.get('id', '')}")
    record(
        "запланированную сессию можно отменить",
        json.loads(snap)["session"]["state"] == "finished",
        json.loads(snap)["session"]["state"],
    )

    # 8. browser agent --------------------------------------------------------
    if args.full:
        meeting = args.meeting
        if meeting is None:
            fixture = ROOT / ".artifacts" / "speech" / "ru-1.wav"
            photo = ROOT / ".artifacts" / "speech" / "face-test.jpg"
            params = "peers=1&name1=Анна Соколова&audio1=/fixtures/ru-1.wav"
            if photo.is_file():
                params += "&face1=/fixtures/face-test.jpg"
            meeting = f"{base}/fake-meeting/?{params}"
            if not fixture.is_file():
                record("агент: фикстура речи найдена", False, "нет .artifacts/speech/ru-1.wav")
                return finish(base)
        status, body, _ = call(
            base,
            "/api/sessions",
            "POST",
            {"meeting_url": meeting, "title": "Приёмка агента", "pipeline": "jazz", "headless": True},
        )
        session_id = json.loads(body).get("id", "") if status == 201 else ""
        record("агент стартует", status == 201, session_id)

        deadline = time.time() + 240
        payload = {}
        last_count = 0
        last_change = time.time()
        while time.time() < deadline:
            _s, body, _h = call(base, f"/api/sessions/{session_id}")
            payload = json.loads(body)
            if payload["session"]["state"] in ("finished", "failed"):
                break
            count = len(payload.get("segments", []))
            if count != last_count:
                last_count, last_change = count, time.time()
            elif count and time.time() - last_change > 8:
                call(base, f"/api/sessions/{session_id}/stop", "POST")
            time.sleep(1.5)

        participants = payload.get("participants", [])
        segments = payload.get("segments", [])
        agent_state = payload.get("session", {}).get("state", "?")
        record("агент вошёл во встречу", len(participants) >= 2, f"{len(participants)} участников, state={agent_state}")
        record(
            "спикер определён по дорожке",
            any(p["display_name"] == "Анна Соколова" for p in participants),
            ", ".join(p["display_name"] for p in participants),
        )
        record("живой транскрипт получен", len(segments) >= 1, f"{len(segments)} реплик")
        record(
            "лицо участника найдено",
            any(p["face_status"] in ("detected", "frame") for p in participants),
            ", ".join(sorted({p["face_status"] for p in participants})),
        )

    return finish(base)


def finish(base: str) -> int:
    passed = sum(1 for item in RESULTS if item["ok"])
    total = len(RESULTS)
    report = {
        "base": base,
        "when": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "total": total,
        "results": RESULTS,
    }
    out = ROOT / ".artifacts" / f"acceptance-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{passed}/{total} проверок пройдено")
    print(f"отчёт: {out}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
