"""Project-local temp directory for Playwright and friends.

Why this exists
---------------
Playwright's driver creates its working folders with ``mkdtemp`` in the system
temp directory. On this machine that fails with ``EPERM`` (the same Windows
restricted-token issue that breaks ``pip``: a directory created with
``mode=0o700`` gets a security descriptor the process then cannot use), and under
the DSH sandbox the system temp is off limits entirely.

Pointing ``TMP``/``TEMP``/``TMPDIR`` at a folder inside the repository before the
driver starts fixes both cases and keeps artefacts next to the project.

Usage, at the very top of any tool that launches a browser::

    from _env import ensure_local_temp
    ensure_local_temp()
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def ensure_local_temp(subdir: str = ".tmp") -> Path:
    target = ROOT / subdir
    target.mkdir(parents=True, exist_ok=True)
    for name in ("TMP", "TEMP", "TMPDIR"):
        os.environ[name] = str(target)
    return target
