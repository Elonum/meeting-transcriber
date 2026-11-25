"""Self-contained SaluteJazz test: one browser hosts a meeting, another joins it.

Why: an anonymous SaluteJazz meeting ends as soon as its creator leaves, so a
link alone is not joinable. This script creates a room, keeps the host page open,
feeds it a synthesized Russian WAV as its microphone, and lets an agent join as a
guest — the whole capture path against the real product, with no manual steps.

    .venv\\Scripts\\python.exe tools\\salutejazz_selftest.py --wav .artifacts\\speech\\ru-1.wav

Writes .artifacts/salutejazz-selftest.json and prints a verdict.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import _env  # noqa: F401 - project-local TMP before Playwright starts
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".artifacts"
HOME = "https://salutejazz.ru/calls/"

PRELUDE = """
try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
try { sessionStorage.setItem('s2b.browserChecker.skipChecking', 'true'); } catch (e) {}
"""

HOOK = r"""
(() => {
  const state = { tracks: [], streams: [], errors: [] };
  window.__probe = state;
  const original = window.RTCPeerConnection;
  window.RTCPeerConnection = function (...args) {
    const pc = new original(...args);
    pc.addEventListener('track', (event) => {
      const stream = event.streams && event.streams[0];
      state.tracks.push({
        id: event.track.id,
        kind: event.track.kind,
        label: event.track.label,
        settings: event.track.getSettings ? event.track.getSettings() : {},
        streamTracks: stream ? stream.getTracks().length : 0,
        at: Date.now(),
      });
      if (stream) state.streams.push({ id: stream.id, audio: stream.getAudioTracks().length, video: stream.getVideoTracks().length });
    });
    return pc;
  };
  window.RTCPeerConnection.prototype = original.prototype;
  window.__snapshot = () => ({
    tracks: state.tracks,
    streams: state.streams,
    videos: Array.from(document.querySelectorAll('video')).map((v) => ({
      w: v.videoWidth, h: v.videoHeight, tracks: v.srcObject ? v.srcObject.getTracks().length : 0,
    })),
    url: location.href,
    body: (document.body.innerText || '').slice(0, 300),
  });
})();
"""


#: The product's own labels contain a soft hyphen ("видео\u00adвстречу"), so text
#: selectors miss them; match on normalised text inside the page instead.
CLICK_BY_TEXT = """
(needle) => {
  const norm = (s) => (s || '').replace(/\\u00ad/g, '').replace(/\\s+/g, ' ').trim();
  const nodes = Array.from(document.querySelectorAll('button, a, [role="button"], [data-testid]'));
  const hit = nodes.find((node) => node.offsetParent && norm(node.innerText).includes(needle));
  if (!hit) return null;
  hit.click();
  return norm(hit.innerText).slice(0, 60);
}
"""


def click_by_text(page, needle: str) -> str | None:
    try:
        return page.evaluate(CLICK_BY_TEXT, needle)
    except Exception:  # noqa: BLE001
        return None


def dismiss(page, labels: list[str]) -> list[str]:
    """Close onboarding toasts; each return value says which label was clicked."""
    done: list[str] = []
    for label in labels:
        clicked = click_by_text(page, label)
        if clicked:
            done.append(clicked)
            page.wait_for_timeout(600)
    return done
    done = []
    for label in labels:
        locator = page.locator(f'button:has-text("{label}")').first
        try:
            if locator.count() and locator.is_visible():
                locator.click(timeout=2000)
                done.append(label)
        except Exception:  # noqa: BLE001
            continue
    return done


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", default=str(ROOT / ".artifacts" / "speech" / "ru-1.wav"))
    parser.add_argument("--seconds", type=int, default=25)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)

    wav = Path(args.wav).resolve()
    if not wav.is_file():
        print("missing wav:", wav)
        return 2

    report: dict = {"wav": str(wav), "steps": [], "host_room": None}

    with sync_playwright() as playwright:
        host_args = [
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={wav}",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled",
        ]
        # --- host: create a room and stay in it --------------------------------
        host_browser = playwright.chromium.launch(
            channel="chrome", headless=not args.headed, args=host_args,
            ignore_default_args=["--enable-automation"],
        )
        host_ctx = host_browser.new_context(
            permissions=["microphone", "camera"], viewport={"width": 1280, "height": 800}, locale="ru-RU"
        )
        host_ctx.add_init_script(PRELUDE)
        host = host_ctx.new_page()
        host.goto(HOME, wait_until="domcontentloaded", timeout=90_000)
        host.wait_for_timeout(3000)
        report["steps"].append(f"dismissed: {dismiss(host, ['Понятно', 'Пропустить', 'Позже'])}")

        clicked = click_by_text(host, "Создать видео")
        report["steps"].append(f"clicked create: {clicked}")
        host.wait_for_timeout(4000)
        report["create_dialog_body"] = host.evaluate("(document.body.innerText || '').slice(0, 400)")

        # A modal may ask to confirm; try the obvious buttons once.
        for label in ("Создать", "Начать", "Продолжить", "Разрешить", "Понятно"):
            result = click_by_text(host, label)
            if result:
                report["steps"].append(f"confirmed: {result}")
                host.wait_for_timeout(1500)

        host.wait_for_timeout(5000)
        report["steps"].append(f"dismissed after create: {dismiss(host, ['Понятно', 'Пропустить', 'Позже', 'Разрешить'])}")
        host.wait_for_timeout(2000)
        report["host_url"] = host.url
        report["host_body"] = host.evaluate("(document.body.innerText || '').slice(0, 500)")

        room = ""
        if "/calls/" in host.url:
            room = host.url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        report["host_room"] = room or None
        print("host url:", host.url, "| room:", room)

        snapshot = {"tracks": [], "streams": [], "videos": []}
        if room and room != "":
            # --- agent: join the same room ------------------------------------
            agent_args = [
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
            ]
            agent_browser = playwright.chromium.launch(
                channel="chrome", headless=not args.headed, args=agent_args,
                ignore_default_args=["--enable-automation"],
            )
            agent_ctx = agent_browser.new_context(
                permissions=["microphone", "camera"], viewport={"width": 1280, "height": 800}, locale="ru-RU"
            )
            agent_ctx.add_init_script(PRELUDE)
            agent_ctx.add_init_script(HOOK)
            agent = agent_ctx.new_page()
            agent.goto(f"https://salutejazz.ru/calls/{room}", wait_until="domcontentloaded", timeout=90_000)
            agent.wait_for_timeout(3500)
            try:
                agent.fill('input[name="name"]', "Ассистент (транскрибация)", timeout=5000)
                report["steps"].append("filled name")
            except Exception as error:  # noqa: BLE001
                report["steps"].append(f"name field: {error}")
            for label in ("Подключиться", "Присоединиться", "Войти в комнату"):
                locator = agent.locator(f'button:has-text("{label}")').first
                try:
                    if locator.count() and locator.is_visible():
                        locator.click(timeout=4000)
                        report["steps"].append(f"clicked {label}")
                        break
                except Exception:  # noqa: BLE001
                    continue

            deadline = time.time() + args.seconds
            while time.time() < deadline:
                agent.wait_for_timeout(3000)
                snapshot = agent.evaluate("window.__snapshot ? window.__snapshot() : {}")
                print(
                    f"  agent tracks={len(snapshot.get('tracks', []))} "
                    f"videos={len(snapshot.get('videos', []))} url={snapshot.get('url', '')[:60]}"
                )
            report["agent_snapshot"] = snapshot
            agent.screenshot(path=str(OUT / "selftest-agent.png"))
            agent_browser.close()

        host.screenshot(path=str(OUT / "selftest-host.png"))
        host_browser.close()

    audio = [track for track in report.get("agent_snapshot", {}).get("tracks", []) if track.get("kind") == "audio"]
    report["verdict"] = {
        "host_created_room": bool(report.get("host_room")),
        "audio_tracks_seen": len(audio),
        "per_participant_audio": len(audio) >= 1,
        "video_tiles": len(report.get("agent_snapshot", {}).get("videos", [])),
        "levels": [round(float(track.get("settings", {}).get("sampleRate", 0))) for track in audio],
    }
    (OUT / "salutejazz-selftest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nverdict:", report["verdict"])
    print("report:", OUT / "salutejazz-selftest.json")
    return 0 if report["verdict"]["per_participant_audio"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
