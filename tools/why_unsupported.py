"""Find why a site says "browser not supported".

Downloads the page's JS bundles and prints the code around the "unsupported"
message, so the actual check (UA, feature, version) is visible instead of guessed.

    python tools/why_unsupported.py https://salutejazz.ru/calls
"""

from __future__ import annotations

import re
import sys
import urllib.request

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

NEEDLES = ["не поддерживается", "not supported", "unsupported", "Chrome, Firefox"]


def fetch(url: str, limit: int = 3_000_000) -> str:
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=40) as response:
        return response.read(limit).decode("utf-8", "replace")


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://salutejazz.ru/calls"
    html = fetch(url)
    print(f"html: {len(html)} bytes")

    scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
    print(f"scripts: {len(scripts)}")
    for source in scripts[:8]:
        print("  ", source)

    for source in scripts:
        if not source.startswith("http"):
            base = url.rsplit("/", 1)[0]
            source = base + "/" + source.lstrip("/")
        try:
            code = fetch(source)
        except Exception as error:  # noqa: BLE001
            print(f"  skip {source}: {error}")
            continue
        for needle in NEEDLES:
            index = code.find(needle)
            if index == -1:
                continue
            print(f"\n=== found {needle!r} in {source} at {index}")
            print(code[max(0, index - 700) : index + 200].replace("\n", " "))
            return 0

    print("\nmessage not found in bundles; checking feature probes in the page shell")
    for probe in ("RTCPeerConnection", "webdriver", "chrome", "userAgentData", "isChrome", "supportedBrowsers"):
        print(f"  {probe}: {html.count(probe)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
