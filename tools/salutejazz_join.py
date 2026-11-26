"""Join a SaluteJazz room like a guest and report what the client says.

Written for the case where the lobby accepts the name and password but the room
never produces media: the client logs everything to the console, and those lines
say exactly which step refused (wrong password, no host, no devices, policy).

    .venv\\Scripts\\python.exe tools\\salutejazz_join.py --room xxxxxx --password a1b2c3d4

Everything is written to .artifacts/salutejazz-join.json: console lines, failed
requests, final URL, visible text.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import _env  # noqa: F401  (sets a project-local TMP before Playwright starts)
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / ".artifacts"

PRELUDE = """
try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
try { sessionStorage.setItem('s2b.browserChecker.skipChecking', 'true'); } catch (e) {}
"""

ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-blink-features=AutomationControlled",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--room", required=True, help="room id, e.g. 71qmtd")
    parser.add_argument("--password", required=True)
    parser.add_argument("--name", default="Ассистент")
    parser.add_argument("--seconds", type=int, default=25)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    url = f"https://salutejazz.ru/calls/{args.room}"
    console: list[str] = []
    failed: list[str] = []
    responses: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome",
            headless=not args.headed,
            args=ARGS,
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context(
            permissions=["microphone", "camera"], viewport={"width": 1440, "height": 900}, locale="ru-RU"
        )
        context.add_init_script(PRELUDE)
        page = context.new_page()
        page.on("console", lambda message: console.append(f"{message.type}: {message.text[:400]}"))
        page.on("pageerror", lambda error: console.append(f"pageerror: {error}"))
        page.on("requestfailed", lambda req: failed.append(f"{req.method} {req.url[:160]} :: {req.failure}"))
        page.on(
            "response",
            lambda resp: responses.append(f"{resp.status} {resp.request.method} {resp.url[:160]}")
            if "/api/" in resp.url and resp.status >= 400
            else None,
        )

        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(3000)

        # Lobby: name + password + connect.
        page.fill('input[name="name"]', args.name)
        page.fill('input[name="password"]', args.password)
        page.click('button:has-text("Подключиться")')
        print("clicked connect")

        deadline = time.time() + args.seconds
        while time.time() < deadline:
            page.wait_for_timeout(2500)
            state = page.evaluate(
                "({url: location.href, text: (document.body.innerText || '').slice(0, 300)})"
            )
            print(f"  url={state['url'][:70]} text={state['text'][:80]!r}")

        report = {
            "url": page.url,
            "body": page.evaluate("(document.body.innerText || '').slice(0, 800)"),
            "console": console[-60:],
            "requests_failed": failed[-20:],
            "http_errors": responses[-20:],
        }
        page.screenshot(path=str(OUT / "salutejazz-join.png"))
        browser.close()

    OUT.mkdir(exist_ok=True)
    (OUT / "salutejazz-join.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("report:", OUT / "salutejazz-join.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
