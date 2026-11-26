"""Delete old meetings, because a recorder that never forgets fills the disk.

Disabled by default (``TRANSCRIBE_RETENTION_DAYS=0``): deleting a user's
recordings is not something a demo should do behind their back. When a positive
number of days is configured, a background sweep runs at startup and then hourly,
removing only finished or failed meetings older than that. Live and scheduled
sessions are never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import TYPE_CHECKING

from . import protocol as p

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .session import Session, SessionManager

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 3600
DAY_MS = 24 * 60 * 60 * 1000


def retention_days() -> int:
    """Days to keep, from the environment; 0 disables the sweep."""
    try:
        return max(0, int(os.environ.get("TRANSCRIBE_RETENTION_DAYS", "0")))
    except ValueError:
        log.warning("retention: TRANSCRIBE_RETENTION_DAYS is not a number, disabled")
        return 0


def is_expired(session: Session, days: int, now_ms: int | None = None) -> bool:
    """True when a finished meeting is older than the policy allows."""
    if days <= 0 or session.state not in p.TERMINAL_STATES:
        return False
    moment = p.now_ms() if now_ms is None else now_ms
    return moment - session.created_at_ms > days * DAY_MS


async def sweep(manager: SessionManager, days: int | None = None) -> list[str]:
    """Delete every expired meeting; returns the ids that were removed."""
    policy = retention_days() if days is None else days
    if policy <= 0:
        return []
    removed: list[str] = []
    for session in list(manager.sessions.values()):
        if not is_expired(session, policy):
            continue
        try:
            if await manager.delete(session.id):
                removed.append(session.id)
        except Exception:  # noqa: BLE001 - one broken folder must not stop the sweep
            log.exception("retention: cannot delete %s", session.id)
    if removed:
        log.info("retention: removed %d meeting(s) older than %d day(s)", len(removed), policy)
    return removed


async def run_forever(manager: SessionManager, interval_s: int = DEFAULT_INTERVAL_S) -> None:
    """Background loop started with the application."""
    while True:
        with contextlib.suppress(Exception):
            await sweep(manager)
        await asyncio.sleep(interval_s)
