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


def test_console_notes_are_redacted(monkeypatch) -> None:
    """A page error may quote the invite URL; the note must not carry the password."""
    from types import SimpleNamespace

    from app.capture import BrowserAgent

    agent = BrowserAgent("https://salutejazz.ru/calls/abc123?psw=SECRET")
    agent._on_console(
        SimpleNamespace(
            type="error",
            text="Failed to fetch https://salutejazz.ru/calls/abc123?psw=SECRET",
        )
    )
    agent._on_console(SimpleNamespace(type="warning", text="The ScriptProcessorNode is deprecated"))

    assert len(agent.notes) == 1
    assert "SECRET" not in agent.notes[0]
    assert "psw=" in agent.notes[0]


# -- meeting links are a boundary -------------------------------------------


def test_a_public_https_link_is_accepted() -> None:
    from app.security import validate_meeting_url

    assert validate_meeting_url("https://salutejazz.ru/calls/abc123?psw=x") == (
        "https://salutejazz.ru/calls/abc123?psw=x"
    )


def test_the_local_test_double_is_allowed_over_http() -> None:
    from app.security import validate_meeting_url

    assert validate_meeting_url("http://127.0.0.1:8000/fake-meeting/?peers=1")
    assert validate_meeting_url("http://localhost:8000/fake-meeting/")


@pytest.mark.parametrize(
    "link",
    [
        "http://example.com/meet",          # http to the outside world
        "file:///etc/passwd",               # not a web link at all
        "javascript:alert(1)",
        "http://169.254.169.254/latest",    # cloud metadata
        "https://10.0.0.5/meet",            # private network
        "https://192.168.1.10/meet",
        "https://user:pass@salutejazz.ru/x",  # credentials in the URL
        "not a link",
    ],
)
def test_dangerous_links_are_refused(link: str) -> None:
    from app.security import validate_meeting_url

    with pytest.raises(ValueError):
        validate_meeting_url(link)


def test_a_private_host_can_be_allowed_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.security import validate_meeting_url

    monkeypatch.setenv("TRANSCRIBE_ALLOW_PRIVATE_HOSTS", "1")

    assert validate_meeting_url("https://10.0.0.5/meet") == "https://10.0.0.5/meet"
