"""Unit-level checks for the real pipeline object.

The browser parts are covered by the opt-in e2e test; here we pin the wiring that
the API relies on: the pipeline name, the shared model, and the diagnostic
screenshot hook (which must never raise when no directory is configured).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.jazz import JazzPipeline


class DeadAgent:
    """A browser that joined and then died — the classic frozen-demo failure."""

    def __init__(self, reason: str = "page crashed") -> None:
        self.notes: list[str] = []
        self._tracks: dict[str, Any] = {}
        self.crashed = True
        self.crash_reason = reason

    async def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return False

    async def chunks(self):
        if False:  # pragma: no cover - makes this an async generator, like the real one
            yield None
        while True:
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        return None

    async def screenshot(self, path: str) -> None:
        return None


def test_pipeline_identity() -> None:
    pipeline = JazzPipeline("https://jazz.sber.ru/meeting/x")
    assert pipeline.name == "jazz"
    assert pipeline.runner is not None
    assert pipeline.detector.name in {"yunet", "insightface", "none"}


def test_artifacts_dir_is_optional() -> None:
    pipeline = JazzPipeline("https://jazz.sber.ru/meeting/x")
    assert asyncio.run(pipeline._screenshot(0, "test")) is None


def test_artifacts_dir_can_be_set() -> None:
    from pathlib import Path

    pipeline = JazzPipeline("https://jazz.sber.ru/meeting/x")
    pipeline.set_artifacts_dir(Path("data/some-session"))
    assert pipeline._artifacts_dir == Path("data/some-session")


def test_model_is_shared_between_pipelines() -> None:
    first = JazzPipeline("https://jazz.sber.ru/a")
    second = JazzPipeline("https://jazz.sber.ru/b")
    # One 1.6 GB model per meeting would make back-to-back demos feel broken.
    assert first.runner is second.runner


def test_pipeline_fails_fast_when_the_browser_dies() -> None:
    """A closed page must end the session, not hang until someone clicks stop."""
    pipeline = JazzPipeline("https://jazz.sber.ru/meeting/x", agent=DeadAgent())
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="браузер"):
            await asyncio.wait_for(pipeline.run(emit, None), timeout=5)

    asyncio.run(scenario())

    notices = [e["message"] for e in events if e["type"] == "notice"]
    assert any("Браузер агента закрылся" in message for message in notices), notices


def test_pipeline_explains_a_join_without_tracks() -> None:
    pipeline = JazzPipeline("https://jazz.sber.ru/meeting/x", agent=DeadAgent("page closed"))
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(pipeline.run(emit, None), timeout=5)

    asyncio.run(scenario())
    notices = [e["message"] for e in events if e["type"] == "notice"]
    assert any("медиапотоков нет" in message for message in notices), notices
