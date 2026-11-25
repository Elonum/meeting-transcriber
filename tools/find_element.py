"""Inspect what element actually carries a piece of text on a page.

Used when a click does not land: prints tag, classes, href, cursor and geometry
for every element whose normalised text matches a needle, deepest first.

    .venv\\Scripts\\python.exe tools\\find_element.py https://salutejazz.ru/calls/ "Создать видео"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _env  # noqa: F401
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / ".artifacts"

PRELUDE = """
try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
try { sessionStorage.setItem('s2b.browserChecker.skipChecking', 'true'); } catch (e) {}
"""

SCRIPT = """
(needle) => {
  const norm = (s) => (s || '').replace(/\\u00ad/g, '').replace(/\\s+/g, ' ').trim();
  const all = Array.from(document.querySelectorAll('*'));
  const hits = [];
  for (const node of all) {
    const text = norm(node.innerText);
    if (!text.includes(needle)) continue;
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    hits.push({
      tag: node.tagName.toLowerCase(),
      cls: (node.className || '').toString().slice(0, 90),
      role: node.getAttribute('role') || '',
      testid: node.getAttribute('data-testid') || '',
      href: node.getAttribute('href') || '',
      cursor: style.cursor,
      text: text.slice(0, 50),
      depth: (() => { let d = 0, p = node; while (p && p.parentElement) { d++; p = p.parentElement; } return d; })(),
      w: Math.round(rect.width),
      h: Math.round(rect.height),
      childrenWithText: Array.from(node.children).filter((c) => norm(c.innerText).includes(needle)).length,
    });
  }
  hits.sort((a, b) => b.depth - a.depth);
  return hits.slice(0, 12);
}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("needle")
    parser.add_argument("--seconds", type=int, default=8)
    args = parser.parse_args()

    OUT.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome", headless=True, ignore_default_args=["--enable-automation"]
        )
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ru-RU")
        context.add_init_script(PRELUDE)
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(args.seconds * 1000)

        hits = page.evaluate(SCRIPT, args.needle)
        (OUT / "element.json").write_text(json.dumps(hits, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(hits, ensure_ascii=False, indent=2)[:1500])
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
