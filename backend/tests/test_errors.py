"""Error reporting: a failure must say what happened, in words a human reads.

The real incident behind these tests: uvicorn --reload on Windows installs the
selector event loop, Playwright cannot spawn its driver, and the exception has an
empty message. The UI showed "Не удалось подключиться: ." and the session ended
in "failed" with no reason at all.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from app.errors import AgentUnavailableError, describe_error
from app.protocol import FAILED
from app.session import Session


def test_describe_error_keeps_a_real_message() -> None:
    assert describe_error(ValueError("ссылка неверная")) == "ссылка неверная"


def test_describe_error_never_returns_empty_text() -> None:
    assert describe_error(NotImplementedError()) == "NotImplementedError (исключение без описания)"
    assert describe_error(TimeoutError()) == "TimeoutError (исключение без описания)"


def test_describe_error_strips_whitespace() -> None:
    assert describe_error(RuntimeError("  сбой  ")) == "сбой"


def test_agent_start_explains_the_selector_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact failure that broke the demo: no more bare NotImplementedError."""
    import playwright.async_api as api
    from app.capture import BrowserAgent

    class BrokenPlaywright:
        async def start(self) -> Any:
            raise NotImplementedError

    monkeypatch.setattr(api, "async_playwright", lambda: BrokenPlaywright())

    agent = BrowserAgent("https://salutejazz.ru/calls/abc123")
    with pytest.raises(AgentUnavailableError) as error:
        asyncio.run(agent.start())

    message = str(error.value)
    assert "SelectorEventLoop" in message
    assert "--reload" in message
    assert "dev.cmd" in message


class BrokenPipeline:
    """A pipeline that dies the way Playwright did: with an empty message."""

    name = "broken"

    async def run(self, emit: Any, deadline_ms: int | None) -> None:
        raise NotImplementedError


def test_failed_session_carries_a_readable_error(tmp_path: Path) -> None:
    session = Session(
        "abc123",
        "https://salutejazz.ru/calls/abc123",
        BrokenPipeline(),  # type: ignore[arg-type]
        1_700_000_000_000,
        data_dir=tmp_path,
    )

    asyncio.run(session._run(1.0))

    assert session.state == FAILED
    assert session.error == "NotImplementedError (исключение без описания)"
    assert "Ошибка: ." not in (session.error or "")
