"""Screenshot the UI with a real browser, so layout bugs are caught, not shipped.

Uses the installed Chrome instead of downloading Chromium.

    .venv\\Scripts\\python.exe tools\\shot.py [base_url]

Saves .artifacts/ui-*.png: welcome screen, live meeting, download menu, narrow
viewport.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import _env  # noqa: F401 - project-local TMP before Playwright starts
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(__file__).resolve().parents[1] / ".artifacts"


def create_session() -> str:
    payload = {
        "meeting_url": "https://salutejazz.ru/calls/pz1mhy?psw=OAQMDB0IFkMdFAsaQhwBUBIeFg",
        "title": "Еженедельная синхронизация",
        "pipeline": "mock",
        "speed": 6,
    }
    request = urllib.request.Request(
        BASE + "/api/sessions",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())["id"]


def main() -> int:
    OUT.mkdir(exist_ok=True)
    session_id = create_session()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome", headless=True, ignore_default_args=["--enable-automation"]
        )
        page = browser.new_page(viewport={"width": 1500, "height": 940}, locale="ru-RU")
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        page.goto(BASE, wait_until="networkidle")
        page.screenshot(path=str(OUT / "ui-0-welcome.png"))

        page.goto(f"{BASE}/?session={session_id}", wait_until="networkidle")
        page.wait_for_selector(".tile", timeout=20000)
        page.wait_for_timeout(7000)
        page.screenshot(path=str(OUT / "ui-1-live.png"))

        page.click(".menu button")
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "ui-2-menu.png"))
        page.keyboard.press("Escape")

        page.wait_for_timeout(5000)
        page.screenshot(path=str(OUT / "ui-3-transcript.png"))

        page.set_viewport_size({"width": 760, "height": 1000})
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "ui-4-narrow.png"), full_page=True)

        browser.close()

    print("console errors:", len(errors))
    for error in errors[:5]:
        print(" ", error)
    print("saved:", sorted(path.name for path in OUT.glob("ui-*.png")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
