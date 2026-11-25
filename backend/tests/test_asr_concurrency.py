"""Concurrency behaviour of the shared Whisper runner.

Several participants are transcribed from different worker threads, and
ctranslate2 models are not thread-safe. These tests pin down the contract:

* finals are serialised — never two inferences at once on one model;
* interim revisions are dropped instead of queueing, so a live transcript cannot
  fall behind by minutes when the GPU is busy.

A fake model is used, so the tests run in milliseconds and need no weights.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
from app.asr import WhisperRunner


class FakeModel:
    """Records the maximum number of concurrent transcriptions it ever saw."""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self._mutex = threading.Lock()

    def transcribe(self, audio, **kwargs):  # noqa: ANN001, ARG002
        with self._mutex:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        with self._mutex:
            self.active -= 1
        return iter([SimpleNamespace(text=" проверка ")]), SimpleNamespace()


def _runner(model: FakeModel) -> WhisperRunner:
    runner = WhisperRunner("tiny")
    runner._model = model  # skip loading real weights
    runner.resolved = "fake"
    return runner


def test_finals_never_run_in_parallel() -> None:
    model = FakeModel()
    runner = _runner(model)
    audio = np.zeros(1600, dtype=np.float32)
    results: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(runner.transcribe(audio))
        except BaseException as error:  # noqa: BLE001 - the test asserts on it
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    assert len(results) == 8 and all(results)
    assert model.max_active == 1, f"{model.max_active} concurrent inferences"


def test_greedy_partial_is_dropped_when_busy() -> None:
    model = FakeModel(delay=0.05)
    runner = _runner(model)
    audio = np.zeros(1600, dtype=np.float32)

    with runner._infer_lock:
        started = time.perf_counter()
        text = runner.transcribe(audio, greedy=True)
        elapsed = time.perf_counter() - started

    assert text == ""
    assert elapsed < 0.02, "a partial must not wait for the final"
    assert model.calls == 0


def test_greedy_partial_runs_when_free() -> None:
    model = FakeModel()
    runner = _runner(model)
    assert runner.transcribe(np.zeros(1600, dtype=np.float32), greedy=True)
    assert model.calls == 1


def test_final_waits_for_the_lock() -> None:
    model = FakeModel(delay=0.01)
    runner = _runner(model)
    audio = np.zeros(1600, dtype=np.float32)
    done = threading.Event()

    def worker() -> None:
        runner.transcribe(audio)
        done.set()

    with runner._infer_lock:
        thread = threading.Thread(target=worker)
        thread.start()
        assert not done.wait(0.1), "final should still be waiting for the lock"
    assert done.wait(5), "final should proceed once the lock is released"
    thread.join(timeout=5)
