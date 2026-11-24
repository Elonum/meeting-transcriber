"""Unit-level checks for the real pipeline object.

The browser parts are covered by the opt-in e2e test; here we pin the wiring that
the API relies on: the pipeline name, the shared model, and the diagnostic
screenshot hook (which must never raise when no directory is configured).
"""

from __future__ import annotations

import asyncio

from app.jazz import JazzPipeline


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
