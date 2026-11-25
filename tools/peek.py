"""Open a page in a real browser, screenshot it and dump what is clickable.

Needed because some sites are only reachable for Chromium/Edge (which trust the
Windows certificate store) and not for Python's certifi bundle — jazz.sber.ru is
exactly such a case. This prints the visible entry points, so the login/creation
flow can be described without guessing.

    .venv\\Scripts\\python.exe tools\\peek.py https://jazz.sber.ru --seconds 12
"""

from __future__ import annotations

import argparse
import json

# Playwright's driver cannot use the system temp on this machine: use a local one.
import sys as _sys
from pathlib import Path
from pathlib import Path as _Path

from playwright.sync_api import sync_playwright

_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from _env import ensure_local_temp as _ensure_local_temp  # noqa: E402

_ensure_local_temp()

OUT = Path(__file__).resolve().parents[1] / ".artifacts"

# Sites that gate on "supported browser" usually check navigator.webdriver and the
# presence of automation flags; Vanilla Playwright fails those checks.
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
]
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--vanilla",
        action="store_true",
        help="no automation flags, no UA override — the closest to a normal user",
    )
    parser.add_argument(
        "--hide-webdriver",
        action="store_true",
        help="only remove navigator.webdriver, keep the real UA and no launch flags",
    )
    parser.add_argument("--channel", default=None, help="msedge | chrome | chromium")
    parser.add_argument(
        "--skip-checker",
        action="store_true",
        help="preset the product's own sessionStorage flag that bypasses its browser gate",
    )
    args = parser.parse_args()

    OUT.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = None
        launch_args = [] if args.vanilla else STEALTH_ARGS
        # Playwright adds --enable-automation by default, which Chat/VC clients
        # detect. Dropping it is the difference between "supported" and not.
        launch_kwargs = {"ignore_default_args": ["--enable-automation"]}
        channels = [args.channel] if args.channel else ["msedge", "chrome", None]
        for channel in channels:
            try:
                browser = (
                    playwright.chromium.launch(
                        channel=channel, headless=not args.headed, args=launch_args, **launch_kwargs
                    )
                    if channel
                    else playwright.chromium.launch(
                        headless=not args.headed, args=launch_args, **launch_kwargs
                    )
                )
                print("browser:", channel or "chromium", "| flags:", len(launch_args))
                break
            except Exception:  # noqa: BLE001
                continue
        if browser is None:
            print("no browser available")
            return 2

        context_kwargs = {"viewport": {"width": 1440, "height": 900}, "locale": "ru-RU"}
        if not args.vanilla:
            context_kwargs["user_agent"] = CHROME_UA
        context = browser.new_context(**context_kwargs)
        if not args.vanilla or args.hide_webdriver:
            context.add_init_script(STEALTH_JS)
        if args.skip_checker:
            # SaluteJazz keeps this flag in sessionStorage and reads it at boot;
            # it is what their own "continue anyway" button writes.
            context.add_init_script(
                "try{sessionStorage.setItem('s2b.browserChecker.skipChecking','true')}catch(e){}"
            )
        page = context.new_page()
        errors: list[str] = []
        net_failed: list[str] = []
        net_bad: list[str] = []
        console: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("requestfailed", lambda req: net_failed.append(f"{req.method} {req.url[:140]} :: {req.failure}"))
        page.on(
            "response",
            lambda resp: net_bad.append(f"{resp.status} {resp.url[:140]}")
            if resp.status >= 400
            else None,
        )
        page.on("console", lambda message: console.append(f"{message.type}: {message.text[:200]}"))

        try:
            response = page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
            print("status:", response.status if response else "?")
        except Exception as error:  # noqa: BLE001
            print("navigation failed:", error)
            print("this usually means the certificate is not trusted by the browser either")
            page.screenshot(path=str(OUT / "peek-failed.png"))
            browser.close()
            return 1

        page.wait_for_timeout(args.seconds * 1000)
        shot = OUT / "peek.png"
        page.screenshot(path=str(shot))

        info = page.evaluate("""
            (() => {
              const text = (node) => (node.innerText || node.textContent || '').trim().replace(/\\s+/g, ' ');
              const buttons = Array.from(document.querySelectorAll('button, a[href], [role="button"]'))
                .map(text).filter((value) => value && value.length < 60);
              return {
                title: document.title,
                url: location.href,
                headings: Array.from(document.querySelectorAll('h1, h2')).map(text).slice(0, 8),
                buttons: Array.from(new Set(buttons)).slice(0, 30),
                inputs: Array.from(document.querySelectorAll('input')).map((input) => input.type + ':' + (input.name || input.placeholder || '')),
                bodyStart: (document.body.innerText || '').slice(0, 700),
              };
            })()
        """)
        info["errors"] = errors[:10]
        info["network_failed"] = net_failed[:15]
        info["network_bad"] = net_bad[:15]
        info["console"] = console[:20]
        print(json.dumps(info, ensure_ascii=False, indent=2))
        # The console mangles Cyrillic on Windows; the file keeps it intact.
        (OUT / "peek.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        print("screenshot:", shot)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
