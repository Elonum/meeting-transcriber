"""Screenshot the UI with a real browser, so layout bugs are caught, not shipped.

Uses the installed Edge/Chrome (``channel=``) instead of the bundled Chromium so
nothing has to be downloaded first.

Run: .venv\\Scripts\\python.exe tools\\shot.py [base_url]
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# Playwright's driver cannot use the system temp on this machine: use a local one.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from _env import ensure_local_temp as _ensure_local_temp  # noqa: E402

_ensure_local_temp()

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(__file__).resolve().parents[1] / ".artifacts"
OUT.mkdir(exist_ok=True)


def main() -> int:
    console: list[str] = []
    with sync_playwright() as playwright:
        last_error: Exception | None = None
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = playwright.chromium.launch(channel=channel) if channel else playwright.chromium.launch()
                print("browser:", channel or "bundled chromium")
                break
            except Exception as error:  # noqa: BLE001 - try the next channel
                last_error = error
        if browser is None:
            print("cannot launch a browser:", last_error)
            return 1

        page = browser.new_page(viewport={"width": 1500, "height": 940}, device_scale_factor=1)
        page.on("console", lambda message: console.append(f"{message.type}: {message.text}"))
        page.on("pageerror", lambda error: console.append(f"pageerror: {error}"))

        page.goto(BASE, wait_until="networkidle")
        page.screenshot(path=str(OUT / "ui-0-welcome.png"))

        page.fill('input[type="url"]', "https://jazz.sber.ru/meeting/weekly-sync")
        page.fill('input[placeholder="Планёрка по транскрибации"]', "Еженедельная синхронизация")
        page.select_option("select", "1")
        page.click("button.primary")

        page.wait_for_selector(".tile", timeout=15000)
        page.wait_for_timeout(7000)
        page.screenshot(path=str(OUT / "ui-1-live.png"))

        page.wait_for_timeout(9000)
        page.screenshot(path=str(OUT / "ui-2-transcript.png"))

        # Narrow viewport check.
        page.set_viewport_size({"width": 760, "height": 1000})
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "ui-3-narrow.png"), full_page=True)

        browser.close()

    print("console messages:", len(console))
    for line in console[:25]:
        print(" ", line)
    print("saved to", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
