"""Log hygiene: a meeting password must never end up in a log file."""

from __future__ import annotations

import pytest
from app.security import redact_text, redact_url


def test_password_in_invite_link_is_masked() -> None:
    url = "https://salutejazz.ru/calls/abc123?psw=OAQMDB0IFkMdFAsaQhwBUBIeFg"
    assert redact_url(url) == "https://salutejazz.ru/calls/abc123?psw=%2A%2A%2A"


def test_link_without_a_password_is_untouched() -> None:
    url = "https://salutejazz.ru/calls/abc123"
    assert redact_url(url) == url


def test_other_parameters_survive() -> None:
    url = "https://salutejazz.ru/calls/abc123?foo=bar&psw=SECRET"
    masked = redact_url(url)
    assert "foo=bar" in masked
    assert "SECRET" not in masked


@pytest.mark.parametrize("key", ["psw", "password", "pwd", "PSW"])
def test_every_known_password_parameter(key: str) -> None:
    assert "SECRET" not in redact_url(f"https://salutejazz.ru/calls/abc?{key}=SECRET")


def test_redact_text_handles_a_sentence() -> None:
    message = "agent: joining https://salutejazz.ru/calls/abc123?psw=SECRET and waiting"
    redacted = redact_text(message)
    assert "SECRET" not in redacted
    assert "joining" in redacted and "waiting" in redacted


def test_garbage_input_is_returned_as_is() -> None:
    assert redact_url("not a url") == "not a url"
    assert redact_text("no links here") == "no links here"
