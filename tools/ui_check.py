"""Layout regression checks driven by a real browser.

A screenshot nobody looks at proves nothing, so this script measures the DOM
instead: overflow, overlaps, tile geometry, autoscroll, export links and the
stop button. Run it after changing CSS.

Run: .venv\\Scripts\\python.exe tools\\ui_check.py [base_url]
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

results: list[tuple[bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((bool(condition), name))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def main() -> int:
    with sync_playwright() as playwright:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = playwright.chromium.launch(channel=channel) if channel else playwright.chromium.launch()
                break
            except Exception:  # noqa: BLE001
                continue
        if browser is None:
            print("no browser available")
            return 2

        page = browser.new_page(viewport={"width": 1500, "height": 940})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)

        page.goto(BASE, wait_until="networkidle")

        # -- welcome state ---------------------------------------------------
        check("welcome heading visible", page.locator(".welcome h1").is_visible())
        check(
            "no horizontal overflow (desktop)",
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
            page.evaluate("`${document.documentElement.scrollWidth} vs ${window.innerWidth}`"),
        )
        check("export buttons disabled without session", page.locator(".export--disabled").count() == 5)

        # -- start a session -------------------------------------------------
        page.fill('input[type="url"]', "https://jazz.sber.ru/meeting/ui-check")
        page.fill('input[placeholder="Планёрка по транскрибации"]', "Проверка интерфейса")
        page.select_option("select", "1")
        page.click("button.primary")

        page.wait_for_selector(".tile", timeout=15000)
        page.wait_for_timeout(9000)

        check("sidebar and workspace do not overlap", page.evaluate("""
            (() => {
              const a = document.querySelector('.sidebar').getBoundingClientRect();
              const b = document.querySelector('.workspace').getBoundingClientRect();
              return a.right <= b.left + 1;
            })()
        """))

        tiles = page.locator(".tile")
        check("four participant tiles", tiles.count() == 4, f"got {tiles.count()}")

        speaking = page.locator(".tile--speaking").count()
        check("an active speaker is highlighted", speaking >= 1, f"{speaking} speaking")

        faces = page.locator(".tile__face:not(.tile__face--none)").count()
        check("faces are recognised and badged", faces == 3, f"{faces} badges (bot has no camera)")

        lines = page.locator(".line").count()
        check("transcript has lines", lines >= 3, f"{lines} lines")

        check("transcript autoscrolls", page.evaluate("""
            (() => {
              const feed = document.querySelector('.transcript__feed');
              return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 48;
            })()
        """))

        check(
            "speaker names rendered in transcript",
            page.locator(".line__speaker").first.inner_text().strip() != "",
            page.locator(".line__speaker").first.inner_text(),
        )

        export_hrefs = page.eval_on_selector_all(".export", "els => els.map(e => e.getAttribute('href'))")
        check("export links point at session files", all(h and "/export/" in h for h in export_hrefs), str(export_hrefs[:2]))

        # -- search ----------------------------------------------------------
        page.fill(".search", "Jazz")
        page.wait_for_timeout(300)
        check("search filters the feed", page.locator(".line").count() < lines or page.locator(".search__count").count() > 0)
        page.fill(".search", "")
        page.wait_for_timeout(200)

        # -- narrow viewport -------------------------------------------------
        page.set_viewport_size({"width": 760, "height": 1000})
        page.wait_for_timeout(400)
        check(
            "no horizontal overflow (narrow)",
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
            page.evaluate("`${document.documentElement.scrollWidth} vs ${window.innerWidth}`"),
        )
        check("stacked layout on narrow screens", page.evaluate("""
            (() => {
              const a = document.querySelector('.sidebar').getBoundingClientRect();
              const b = document.querySelector('.workspace').getBoundingClientRect();
              return a.bottom <= b.top + 2 || a.left < b.left + 2;
            })()
        """))

        # -- stop ------------------------------------------------------------
        page.set_viewport_size({"width": 1500, "height": 940})
        page.click("button.danger")
        try:
            page.wait_for_function(
                "() => document.querySelector('.pill')?.textContent?.includes('Завершено')",
                timeout=6000,
            )
            stopped = True
        except Exception:  # noqa: BLE001
            stopped = False
        state_text = page.locator(".pill").inner_text().strip()
        check("stop finishes the session", stopped, state_text)

        browser.close()

    real_errors = [e for e in errors if "favicon" not in e.lower()]
    check("no console errors", not real_errors, "; ".join(real_errors[:3]))

    failed = [name for ok, name in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
