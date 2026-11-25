"""Run the product's BrowserAgent against a live room and print what it sees.

Isolates the agent from the pipeline: creates a host room (subprocess), starts
``BrowserAgent`` exactly as the product does, then dumps the URL, page text and
join notes. Use it whenever the live session says "no tracks".

    .venv\\Scripts\\python.exe tools\\debug_agent.py --seconds 40
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools"))

import _env  # noqa: E402,F401 - project-local TMP before Playwright starts

STATE = ROOT / ".artifacts" / "host-room.json"


async def run(seconds: int) -> int:
    from app.capture import BrowserAgent

    STATE.parent.mkdir(exist_ok=True)
    if STATE.exists():
        STATE.unlink()

    host = subprocess.Popen(  # noqa: ASYNC220 - a debug tool, the blocking call is fine
        [sys.executable, str(ROOT / "tools" / "host_room.py"), "--minutes", "3"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    room_url = None
    deadline = time.time() + 75
    while time.time() < deadline:
        if STATE.exists() and STATE.stat().st_size > 0:
            room_url = json.loads(STATE.read_text(encoding="utf-8"))["url"]
            break
        await asyncio.sleep(2)
    if not room_url:
        host.terminate()
        print("host failed")
        return 1
    print("room:", room_url)

    agent = BrowserAgent(room_url, headless=True)
    try:
        await agent.start()
        print("url:", agent.page.url)
        print("notes:")
        for note in agent.notes[-12:]:
            print("  ", note)
        print("tracks:", {tid: info.name for tid, info in agent._tracks.items()})
        body = await agent.page.evaluate("(document.body.innerText || '').slice(0, 400)")
        (ROOT / ".artifacts" / "agent-debug-body.txt").write_text(body, encoding="utf-8")
        print("body saved to .artifacts/agent-debug-body.txt")
        print("participants:", agent._remote_names)

        chunks = 0
        end = time.time() + seconds
        while time.time() < end:
            try:
                await asyncio.wait_for(agent._queue.get(), timeout=2)
                chunks += 1
            except TimeoutError:
                continue
        print("chunks:", chunks)
        await agent.screenshot(str(ROOT / ".artifacts" / "agent-debug.png"))
    finally:
        await agent.close()
        host.terminate()
        try:
            host.wait(timeout=10)
        except Exception:  # noqa: BLE001
            host.kill()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()
    return asyncio.run(run(args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
