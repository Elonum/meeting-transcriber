"""Keep a SaluteJazz meeting alive and speaking, for testing the real pipeline.

An anonymous SaluteJazz room dies when its creator leaves, so a link alone is not
joinable. This script creates a room in a real browser, feeds a synthesized WAV
into its microphone, and stays inside — long enough for the agent under test to
join and transcribe.

    .venv\\Scripts\\python.exe tools\\host_room.py --minutes 3

Writes .artifacts/host-room.json with the room URL (including ?psw=) as soon as
the room exists, so another process can pick it up.
"""

from __future__ import annotations

import argparse
import json
import re
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

CLICK_BY_TEXT = """
(needle) => {
  const norm = (s) => (s || '').replace(/\\u00ad/g, '').replace(/\\s+/g, ' ').trim();
  const visible = (node) => {
    const rect = node.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    const style = window.getComputedStyle(node);
    return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
  };
  const clickable = (node) => {
    const tag = node.tagName.toLowerCase();
    if (tag === 'button' || tag === 'a' || node.getAttribute('role') === 'button') return true;
    return window.getComputedStyle(node).cursor === 'pointer';
  };
  const depth = (node) => { let d = 0, p = node; while (p && p.parentElement) { d++; p = p.parentElement; } return d; };
  const candidates = Array.from(document.querySelectorAll('*'))
    .filter((node) => visible(node) && clickable(node) && norm(node.innerText).includes(needle))
    .sort((a, b) => depth(b) - depth(a));
  if (!candidates.length) return null;
  candidates[0].scrollIntoView({block: 'center'});
  candidates[0].click();
  return norm(candidates[0].innerText).slice(0, 60);
}
"""

OVERLAYS = ["Пропустить", "Понятно", "Позже", "Разрешить"]


def click_text(page, needle: str, *, timeout: int = 18) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for label in OVERLAYS:
            try:
                page.evaluate(CLICK_BY_TEXT, label)
            except Exception:  # noqa: BLE001
                pass
        try:
            result = page.evaluate(CLICK_BY_TEXT, needle)
        except Exception:  # noqa: BLE001
            result = None
        if result:
            return result
        page.wait_for_timeout(800)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", default=str(ROOT / ".artifacts" / "speech" / "ru-1.wav"))
    parser.add_argument("--minutes", type=float, default=3)
    parser.add_argument("--title", default="Автотест транскрибации")
    args = parser.parse_args()

    wav = Path(args.wav)
    if not wav.is_file():
        print("missing wav:", wav)
        return 2
    OUT.mkdir(exist_ok=True)
    state = OUT / "host-room.json"
    if state.exists():
        state.unlink()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome",
            headless=True,
            args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={wav.resolve()}",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
            ],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context(
            permissions=["microphone", "camera"], viewport={"width": 1280, "height": 800}, locale="ru-RU"
        )
        context.add_init_script(PRELUDE)
        page = context.new_page()

        page.goto(HOME, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(2500)
        print("create:", click_text(page, "Создать видео"))

        page.wait_for_timeout(2500)
        for selector, value in (
            ('input[name="userName"]', "Тестовый хост"),
            ('input[name="conferenceName"]', args.title),
        ):
            try:
                field = page.locator(selector).first
                if field.count():
                    field.fill(value, timeout=3000)
            except Exception:  # noqa: BLE001
                pass
        print("join:", click_text(page, "Создать и присоединиться"))

        deadline = time.time() + 40
        room = None
        while time.time() < deadline:
            match = re.search(r"/calls/([a-z0-9]{6})", page.url)
            if match:
                room = match.group(1)
                break
            page.wait_for_timeout(1500)
        if not room:
            print("room was not created, url:", page.url)
            browser.close()
            return 1

        page.wait_for_timeout(2500)
        # The room UI is only real once the microphone toggle exists; without
        # this check the script happily reports a room that nobody is inside.
        try:
            page.wait_for_selector('[data-testid="microphone"]', timeout=25_000)
            in_room = True
        except Exception:  # noqa: BLE001
            in_room = False
        if not in_room:
            print("host did not reach the room UI, url:", page.url)
            print("body:", page.evaluate("(document.body.innerText || '').slice(0, 300)"))
            page.screenshot(path=str(OUT / "host-failed.png"))
            browser.close()
            return 1

        # Publish audio: while everyone is muted the SFU does not connect.
        for selector in ('[data-testid="microphone"]',):
            try:
                button = page.locator(selector).first
                if button.count():
                    aria = button.get_attribute("aria-label") or ""
                    if "Включить" in aria:
                        button.click()
                        print("unmuted")
            except Exception:  # noqa: BLE001
                pass

        payload = {"url": page.url, "room": room, "created": time.time(), "wav": str(wav)}
        state.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("room ready:", page.url)

        until = time.time() + args.minutes * 60
        while time.time() < until:
            page.wait_for_timeout(5000)
        browser.close()

    print("host finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
