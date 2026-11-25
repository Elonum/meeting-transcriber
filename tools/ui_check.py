"""Layout regression checks driven by a real browser.

A screenshot nobody looks at proves nothing, so this script measures the DOM:
overflow, overlaps, tile geometry, autoscroll, the download menu and the cancel
path for a scheduled meeting. Run it after changing CSS or components.

    .venv\\Scripts\\python.exe tools\\ui_check.py [base_url]

The scripted session is created through the API, so the UI itself stays free of
demo switches: the check opens the same shareable link a user would.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta

import _env  # noqa: F401 - project-local TMP before Playwright starts
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
MEETING = "https://salutejazz.ru/calls/uitest?psw=AAAAAAAAAAAAAAAA"

results: list[tuple[bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((bool(condition), name))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def api(path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
    return json.loads(body) if body else {}


def main() -> int:
    # A session to look at: the same object the form produces, created via API.
    mock = api(
        "/api/sessions",
        {"meeting_url": MEETING, "title": "Проверка интерфейса", "pipeline": "mock", "speed": 6},
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome", headless=True, ignore_default_args=["--enable-automation"]
        )
        page = browser.new_page(viewport={"width": 1500, "height": 940})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)

        # -- welcome ---------------------------------------------------------
        page.goto(BASE, wait_until="networkidle")
        check("welcome heading", page.locator(".welcome h1").is_visible())
        check("three guidance steps", page.locator(".steps li").count() == 3)
        check(
            "no horizontal overflow (desktop)",
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
            page.evaluate("`${document.documentElement.scrollWidth} vs ${window.innerWidth}`"),
        )
        check("no pipeline switch left in the form", page.locator("select").count() == 0)
        check("field named after the meeting address", "Адрес видеовстречи" in page.locator(".form").inner_text())

        # -- live session through the shareable link -------------------------
        page.goto(f"{BASE}/?session={mock['id']}", wait_until="networkidle")
        page.wait_for_selector(".tile", timeout=20000)
        check("session link opens the meeting view", page.locator(".topbar h1").count() == 1)
        check("room code is shown", "uitest" in page.locator(".topbar__room").inner_text().lower())

        page.wait_for_function("document.querySelectorAll('.line').length >= 3", timeout=20000)
        check("four tiles (bot + three people)", page.locator(".tile").count() == 4, str(page.locator(".tile").count()))

        try:
            page.wait_for_selector(".tile--speaking", timeout=8000)
            speaking = page.locator(".tile--speaking").count()
        except Exception:  # noqa: BLE001 - a pause between phrases is not a bug
            speaking = 0
        check("active speaker is highlighted", speaking >= 1, f"{speaking}")

        faces = page.locator(".tile__badge:not(.tile__badge--muted)").count()
        check("faces are badged", faces == 3, f"{faces}")

        check("timer is shown", page.locator(".timer").count() == 1)
        check(
            "transcript autoscrolls",
            page.evaluate(
                "(() => { const f = document.querySelector('.transcript__feed');"
                " return f.scrollHeight - f.scrollTop - f.clientHeight < 48; })()"
            ),
        )
        check(
            "sidebar and workspace do not overlap",
            page.evaluate(
                "(() => { const a = document.querySelector('.sidebar').getBoundingClientRect();"
                " const b = document.querySelector('.workspace').getBoundingClientRect();"
                " return a.right <= b.left + 1; })()"
            ),
        )

        # -- download menu ---------------------------------------------------
        page.click(".menu button")
        page.wait_for_timeout(300)
        items = page.locator(".menu__panel .menu__item").count()
        check("download menu lists every format", items == 6, f"{items} items")
        hrefs = page.eval_on_selector_all(".menu__item", "els => els.map(e => e.getAttribute('href'))")
        check("menu links point at exports", all(h and "/export/" in h for h in hrefs[:5]), str(hrefs[:2]))
        page.keyboard.press("Escape")

        # -- search ----------------------------------------------------------
        page.fill(".search", "Jazz")
        page.wait_for_timeout(300)
        check("search filters the feed", page.locator(".search__count").count() == 1)
        page.fill(".search", "")

        # -- narrow viewport -------------------------------------------------
        page.set_viewport_size({"width": 760, "height": 1000})
        page.wait_for_timeout(400)
        check(
            "no horizontal overflow (narrow)",
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
            page.evaluate("`${document.documentElement.scrollWidth} vs ${window.innerWidth}`"),
        )
        page.set_viewport_size({"width": 1500, "height": 940})

        # -- scheduled session -----------------------------------------------
        start_at = (datetime.now() + timedelta(minutes=30)).isoformat()
        scheduled = api(
            "/api/sessions",
            {"meeting_url": MEETING, "title": "Запланированная", "pipeline": "mock", "start_at": start_at},
        )
        page.goto(f"{BASE}/?session={scheduled['id']}", wait_until="networkidle")
        try:
            page.wait_for_selector(".pill--scheduled", timeout=10000)
            is_scheduled = True
        except Exception:  # noqa: BLE001
            is_scheduled = False
        check("scheduled session shows its state", is_scheduled)
        check("scheduled banner explains the wait", page.locator(".banner--info").count() >= 1)
        check(
            "cancel replaces stop for a scheduled meeting",
            page.locator("button.danger").inner_text().strip() == "Отменить",
        )
        page.click("button.danger")
        try:
            page.wait_for_function(
                "() => document.querySelector('.pill')?.textContent?.includes('Завершено')", timeout=8000
            )
            cancelled = True
        except Exception:  # noqa: BLE001
            cancelled = False
        check("cancelling finishes the session", cancelled, page.locator(".pill").inner_text().strip())

        browser.close()

    real_errors = [error for error in errors if "favicon" not in error.lower()]
    check("no console errors", not real_errors, "; ".join(real_errors[:3]))

    failed = [name for ok, name in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
