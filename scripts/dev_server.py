"""Supervise the API and restart it when Python files change.

Why not ``uvicorn --reload``
----------------------------
On Windows, uvicorn switches to ``SelectorEventLoop`` when it runs with
``--reload`` (see ``uvicorn.loops.asyncio``). The selector loop cannot start
subprocesses, and Playwright starts its driver as a subprocess — so every join
died with a bare ``NotImplementedError`` while the UI said only "встреча
завершена".

Here the API runs as a child process **without** ``--reload``: it gets the
default Proactor loop and can drive a browser. This script only watches the
backend and restarts the child, which gives the same save-and-reload workflow.

    .venv\\Scripts\\python.exe scripts\\dev_server.py --port 8000
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from watchfiles import PythonFilter, watch

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def command(host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "info",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    cmd = command(args.host, args.port)
    print(f"supervisor: {' '.join(cmd)}", flush=True)
    child = subprocess.Popen(cmd, cwd=BACKEND)
    try:
        for changes in watch(BACKEND, watch_filter=PythonFilter()):
            print(f"supervisor: {len(changes)} change(s), restarting", flush=True)
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
            child = subprocess.Popen(cmd, cwd=BACKEND)
    except KeyboardInterrupt:
        pass
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
    return child.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
