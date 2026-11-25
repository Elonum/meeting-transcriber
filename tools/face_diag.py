"""Why is there no face: check what video the room actually sends us.

Joins a meeting as the agent, then reports the peer connections, the tracks they
deliver and whether a frame can be grabbed for each participant. Answers the
question "is the person's camera reaching us at all" without guessing.

    .venv\\Scripts\\python.exe tools\\face_diag.py --url "<адрес видеовстречи>" --seconds 30

Exit code is 0 when at least one frame was captured.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import _env  # noqa: F401 - project-local TMP before Playwright starts

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.capture import BrowserAgent  # noqa: E402


async def run(url: str, seconds: int) -> int:
    agent = BrowserAgent(url, headless=True)
    try:
        await agent.start()
        print("вход:", "; ".join(agent.notes[-5:]) or "без замечаний")
        await asyncio.sleep(seconds)

        diagnostics = await agent.diagnostics()
        peers = diagnostics.get("peers", [])
        print(f"\nсоединений: {len(peers)}")
        for peer in peers:
            print(f"  {peer.get('connection')}: {', '.join(peer.get('receivers', [])) or 'приёмников нет'}")

        videos = diagnostics.get("videos", [])
        print(f"\nвидеоэлементов: {len(videos)}")
        for video in videos:
            print(f"  {video.get('width')}x{video.get('height')} — {', '.join(video.get('tracks', [])) or 'пусто'}")

        frames = 0
        print("\nкадры по участникам:")
        for track_id, info in agent._tracks.items():
            frame = await agent.grab_face(track_id)
            if frame:
                frames += 1
            print(f"  {info.name}: {'кадр ' + str(len(frame)) + ' байт' if frame else 'кадра нет'}")

        if not frames:
            print(
                "\nНи одного кадра. Проверьте: включена ли камера у человека, "
                "и есть ли в списке соединений дорожка video:."
            )
        return 0 if frames else 1
    finally:
        await agent.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="адрес видеовстречи из приглашения")
    parser.add_argument("--seconds", type=int, default=30, help="сколько ждать после входа")
    args = parser.parse_args()
    print("диагностика камеры:", json.dumps({"url": args.url.split("?")[0], "seconds": args.seconds}, ensure_ascii=False))
    return asyncio.run(run(args.url, args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
