"""Retention: old meetings are removed, live ones are not.

Deleting recordings is destructive, so the policy is off unless explicit and only
ever touches meetings that are already over.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app import protocol as p
from app.mock import MockPipeline
from app.retention import DAY_MS, is_expired, retention_days, sweep
from app.session import Session, SessionManager


def make(manager: SessionManager, *, state: str, age_days: float = 0.0) -> Session:
    session = Session(
        f"{abs(hash((state, age_days))) & 0xFFFFFFFFFFFF:012x}",
        "https://salutejazz.ru/calls/abc123",
        MockPipeline("https://salutejazz.ru/calls/abc123"),
        p.now_ms(),
        data_dir=manager.data_dir,
    )
    session.dir.mkdir(parents=True, exist_ok=True)
    (session.dir / "events.jsonl").write_text("", encoding="utf-8")
    session.state = state
    session.created_at_ms -= int(age_days * DAY_MS)
    manager.sessions[session.id] = session
    return session


@pytest.fixture()
def manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path)


def test_policy_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSCRIBE_RETENTION_DAYS", raising=False)
    assert retention_days() == 0
    assert asyncio.run(sweep(SessionManager(Path("unused")))) == []


def test_policy_reads_days_and_survives_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBE_RETENTION_DAYS", "7")
    assert retention_days() == 7
    monkeypatch.setenv("TRANSCRIBE_RETENTION_DAYS", "-3")
    assert retention_days() == 0
    monkeypatch.setenv("TRANSCRIBE_RETENTION_DAYS", "много")
    assert retention_days() == 0


def test_only_finished_sessions_expire(manager: SessionManager) -> None:
    finished = make(manager, state=p.FINISHED, age_days=10)
    live = make(manager, state=p.LIVE, age_days=10)
    scheduled = make(manager, state=p.SCHEDULED, age_days=10)

    assert is_expired(finished, days=7)
    assert not is_expired(live, days=7)
    assert not is_expired(scheduled, days=7)


def test_sweep_removes_old_finished_meetings(manager: SessionManager) -> None:
    old = make(manager, state=p.FINISHED, age_days=30)
    fresh = make(manager, state=p.FINISHED, age_days=1)
    live = make(manager, state=p.LIVE, age_days=30)

    removed = asyncio.run(sweep(manager, days=7))

    assert removed == [old.id]
    assert old.id not in manager.sessions
    assert not old.dir.exists(), "the folder must go with the record"
    assert fresh.id in manager.sessions
    assert live.id in manager.sessions


def test_zero_days_means_never(manager: SessionManager) -> None:
    old = make(manager, state=p.FINISHED, age_days=365)

    assert asyncio.run(sweep(manager, days=0)) == []
    assert old.id in manager.sessions


def test_sweep_is_idempotent(manager: SessionManager) -> None:
    make(manager, state=p.FINISHED, age_days=30)

    first = asyncio.run(sweep(manager, days=7))
    second = asyncio.run(sweep(manager, days=7))

    assert len(first) == 1
    assert second == []


def test_failed_sessions_are_cleaned_too(manager: SessionManager) -> None:
    failed = make(manager, state=p.FAILED, age_days=30)

    assert asyncio.run(sweep(manager, days=7)) == [failed.id]
