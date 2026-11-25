"""Error text for humans: never empty, always says what happened."""

from __future__ import annotations


class AgentUnavailableError(RuntimeError):
    """The browser could not even start, so the meeting cannot be joined."""


def describe_error(error: BaseException) -> str:
    """``str(error)`` is empty for some exceptions (``NotImplementedError``, timeouts).

    An empty message in the UI ("Ошибка: .") is worse than none, so fall back to
    the exception type, which at least points at the layer that failed.
    """
    text = str(error).strip()
    if text:
        return text
    name = error.__class__.__name__
    return f"{name} (исключение без описания)"
