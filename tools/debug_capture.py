"""Debug the browser capture path without the API in the way.

Serves tools/fake_meeting over plain HTTP, points BrowserAgent at it and prints
what the agent sees: page errors, track events, audio chunk counts, page state.
Use it when the e2e test fails and you need to know whether the problem is the
page, the hook or the bindings.

Run (needs full access: Playwright is blocked inside the sandbox):
    .venv\\Scripts\\python.exe tools\\debug_capture.py [--seconds 20] [--headed]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

PORT = 8811


class Handler(SimpleHTTPRequestHandler):
    """Serves the fake meeting, and /fixtures from .artifacts/speech."""

    def translate_path(self, path: str) -> str:  # noqa: D102 - stdlib hook
        clean = path.split("?", 1)[0]
        if clean.startswith("/fixtures/"):
            return str(ROOT / ".artifacts" / "speech" / clean[len("/fixtures/") :])
        if clean in ("/", ""):
            return str(ROOT / "tools" / "fake_meeting" / "index.html")
        return str(ROOT / "tools" / "fake_meeting" / clean.lstrip("/"))

    def log_message(self, *args) -> None:
        return


async def main(seconds: int, headed: bool) -> int:
    from app.capture import BrowserAgent

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{PORT}/?peers=1&audio1=/fixtures/ru-1.wav&name1=Анна Соколова"
    agent = BrowserAgent(url, headless=not headed)
    chunks = 0
    total_bytes = 0
    try:
        await agent.start()
        print("tracks:", {track: info.name for track, info in agent._tracks.items()})
        print("notes:", agent.notes)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            try:
                chunk = await asyncio.wait_for(agent._queue.get(), timeout=2.0)
            except TimeoutError:
                continue
            chunks += 1
            total_bytes += len(chunk.pcm)
            if chunks % 10 == 0:
                print(f"  chunks={chunks} audio={total_bytes / 32000:.1f}s track={chunk.track_id[:8]}")

        print(f"total: chunks={chunks} audio={total_bytes / 32000:.1f}s")

        try:
            state = await agent.page.evaluate(
                "window.__transcribeAgent ? {tracks: window.__transcribeAgent.tracks.size, "
                "contexts: window.__transcribeAgent.contexts.size, "
                "errors: window.__transcribeAgent.errors.slice(0, 5)} : null"
            )
            print("page state:", state)
            videos = await agent.page.evaluate(
                "Array.from(document.querySelectorAll('video')).map(v => "
                "({w: v.videoWidth, h: v.videoHeight, tracks: v.srcObject ? v.srcObject.getTracks().length : 0}))"
            )
            print("videos:", videos)
            status = await agent.page.evaluate("document.getElementById('status')?.textContent")
            print("page status:", status)
        except Exception as error:  # noqa: BLE001
            print("evaluate failed:", error)
    finally:
        await agent.close()
        server.shutdown()
    return 0 if chunks else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.seconds, args.headed)))
