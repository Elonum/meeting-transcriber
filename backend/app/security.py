"""Small security helpers: never write a meeting password into a log.

An invite link carries the password in ``?psw=``. The session folder needs the
link to resume after a restart, but the service log is read over shoulders and
attached to bug reports, so it must not.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE_PARAMS = ("psw", "password", "pwd")


def redact_url(url: str) -> str:
    """``https://…/calls/abc123?psw=SECRET`` → ``…?psw=***``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(key.lower() in SENSITIVE_PARAMS for key, _ in query):
        return url
    masked = [(key, "***" if key.lower() in SENSITIVE_PARAMS else value) for key, value in query]
    return urlunsplit(parts._replace(query=urlencode(masked)))


def redact_text(text: str) -> str:
    """Mask every password-looking parameter inside a longer message."""
    if "psw=" not in text and "password=" not in text:
        return text
    out: list[str] = []
    for token in text.split(" "):
        out.append(redact_url(token))
    return " ".join(out)
