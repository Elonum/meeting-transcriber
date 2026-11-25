"""Layout and behaviour checks driven by a real browser.

Measures the DOM instead of eyeballing screenshots: overflow at three widths,
theme switching and persistence, the download menu, history search and the
two-step delete, plus the scheduled-meeting path.

    .venv\\Scripts\\python.exe tools\\ui_check.py [base_url]

The scripted session is created through the API, so the UI itself stays free of
demo switches: the check opens the same shareable link a user would.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
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


def api(path: str, payload: dict | None = None):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        return error.code, {}


def no_overflow(page) -> tuple[bool, str]:
    width = page.evaluate("document.documentElement.scrollWidth")
    window = page.evaluate("window.innerWidth")
    return width <= window + 1, f"{width} vs {window}"


def main() -> int:
    _, mock = api(
        "/api/sessions",
        {"meeting_url": MEETING, "title": "Проверка интерфейса", "pipeline": "mock", "speed": 6},
    )
    _, throwaway = api(
        "/api/sessions",
        {"meeting_url": "https://salutejazz.ru/calls/delme1", "title": "Удаляемая встреча", "pipeline": "mock", "speed": 30},
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
        ok, detail = no_overflow(page)
        check("no horizontal overflow (desktop)", ok, detail)
        selects = page.locator(".form select")
        check(
            "only the language select remains in the form",
            selects.count() == 1 and selects.first.get_attribute("aria-label") == "Язык речи",
            f"{selects.count()} selects",
        )
        check("speech language defaults to automatic", selects.first.input_value() == "auto")
        check("field named after the meeting address", "Адрес видеовстречи" in page.locator(".form").inner_text())

        # -- theme -----------------------------------------------------------
        before = page.evaluate("document.documentElement.dataset.theme")
        page.click(".theme-toggle")
        page.wait_for_timeout(250)
        after = page.evaluate("document.documentElement.dataset.theme")
        check("theme toggle flips the theme", before != after, f"{before} -> {after}")
        check(
            "theme is persisted",
            page.evaluate("localStorage.getItem('transcribe.theme')") == after,
            after,
        )
        background = page.evaluate("getComputedStyle(document.body).backgroundColor")
        expected = "rgb(244, 246, 251)" if after == "light" else "rgb(10, 12, 17)"
        check("theme paints the page", background == expected, f"{background}")

        page.reload(wait_until="networkidle")
        check(
            "theme survives a reload",
            page.evaluate("document.documentElement.dataset.theme") == after,
        )
        page.click(".theme-toggle")  # back to the previous theme for screenshots

        # -- live session through the shareable link -------------------------
        page.goto(f"{BASE}/?session={mock['id']}", wait_until="networkidle")
        page.wait_for_selector(".tile", timeout=20000)
        check("session link opens the meeting view", page.locator(".topbar h1").count() == 1)
        check("room code is shown", "uitest" in page.locator(".topbar__room").inner_text().lower())

        page.wait_for_function("document.querySelectorAll('.line').length >= 3", timeout=20000)
        check("four tiles (bot + three people)", page.locator(".tile").count() == 4, str(page.locator(".tile").count()))

        try:
            page.wait_for_selector(".tile--speaking", timeout=20000)
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
        page.wait_for_timeout(400)
        items = page.locator(".menu__panel .menu__item").count()
        check("download menu offers archive plus formats", items >= 7, f"{items} items")
        hrefs = page.eval_on_selector_all(".menu__item", "els => els.map(e => e.getAttribute('href'))")
        check("menu has a ZIP bundle", any(h and h.endswith("/export/zip") for h in hrefs), str(hrefs[:2]))
        check("menu links point at exports", all(h and "/export/" in h for h in hrefs[:6]), str(hrefs[:2]))
        page.keyboard.press("Escape")

        # -- typography: the UI must stay readable ---------------------------
        sizes = page.evaluate(
            """
            () => {
              const rows = [];
              for (const el of document.querySelectorAll('body *')) {
                const style = getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                if (el.children.length) continue;
                const text = (el.textContent || '').trim();
                if (!text) continue;
                rows.push({ label: (el.className || el.tagName).toString().slice(0, 40), size: parseFloat(style.fontSize) });
              }
              return rows;
            }
            """
        )
        smallest = min(sizes, key=lambda row: row["size"]) if sizes else {"label": "-", "size": 0}
        body_size = page.evaluate("parseFloat(getComputedStyle(document.querySelector('.line__text')).fontSize)")
        tile_size = page.evaluate("parseFloat(getComputedStyle(document.querySelector('.tile__name')).fontSize)")
        check("transcript text is 15 px or larger", body_size >= 15, f"{body_size} px")
        check("participant names are 13.5 px or larger", tile_size >= 13.5, f"{tile_size} px")
        check("no visible text below 11 px", smallest["size"] >= 11, f"{smallest['size']} px in .{smallest['label']}")

        # -- search inside the transcript ------------------------------------
        page.fill(".search", "Jazz")
        page.wait_for_timeout(300)
        check("transcript search filters", page.locator(".search__count").count() == 1)
        page.fill(".search", "")

        # -- responsive ------------------------------------------------------
        for width in (1024, 760, 420):
            page.set_viewport_size({"width": width, "height": 1000})
            page.wait_for_timeout(350)
            ok, detail = no_overflow(page)
            check(f"no horizontal overflow ({width}px)", ok, detail)

        page.set_viewport_size({"width": 420, "height": 900})
        page.wait_for_timeout(300)
        check(
            "stage scrolls sideways on a phone",
            page.evaluate(
                "(() => { const t = document.querySelector('.tiles');"
                " return t.scrollWidth >= t.clientWidth; })()"
            ),
        )
        check(
            "header stays visible while scrolling",
            page.evaluate("getComputedStyle(document.querySelector('.topbar')).position") == "sticky",
        )

        page.set_viewport_size({"width": 1500, "height": 940})

        # -- history search and delete ---------------------------------------
        page.goto(BASE, wait_until="networkidle")
        page.fill(".history__search", "Удаляемая")
        page.wait_for_timeout(700)
        rows = page.locator(".history__row").count()
        _, found = api("/api/sessions?q=" + urllib.parse.quote("Удаляемая"))
        check(
            "history search matches the API",
            rows == found.get("total") and rows >= 1,
            f"{rows} rows vs {found.get('total')}",
        )

        row = page.locator(".history__row", has_text="Удаляемая").first
        row.locator(".history__delete").click()
        page.wait_for_timeout(200)
        check("delete asks for confirmation", row.locator(".history__danger").count() == 1)
        row.locator(".history__danger").click()
        page.wait_for_timeout(1200)
        status, _ = api(f"/api/sessions/{throwaway['id']}")
        check("delete removes the meeting", status == 404, f"HTTP {status}")

        page.fill(".history__search", "")
        page.wait_for_timeout(600)
        check("clearing the search restores the list", page.locator(".history__row").count() >= 1)

        # -- scheduled session ----------------------------------------------
        start_at = (datetime.now() + timedelta(minutes=30)).isoformat()
        _, scheduled = api(
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

        # -- live lines disappear once the phrase is finalised ----------------
        _, finished = api(
            "/api/sessions",
            {"meeting_url": MEETING, "title": "Финальные реплики", "pipeline": "mock", "speed": 40},
        )
        page.goto(f"{BASE}/?session={finished['id']}", wait_until="networkidle")
        try:
            page.wait_for_function(
                "() => document.querySelector('.pill')?.textContent?.includes('Завершено')", timeout=60000
            )
            page.wait_for_timeout(800)
            partials = page.locator(".line--partial").count()
            lines = page.locator(".line").count()
        except Exception:  # noqa: BLE001
            partials, lines = -1, 0
        check("no stale live lines after finals", partials == 0, f"{partials} partials, {lines} lines")

        browser.close()

    real_errors = [error for error in errors if "favicon" not in error.lower()]
    check("no console errors", not real_errors, "; ".join(real_errors[:3]))

    failed = [name for ok, name in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
