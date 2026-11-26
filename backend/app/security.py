"""Small security helpers: never write a meeting password into a log.

An invite link carries the password in ``?psw=``. The session folder needs the
link to resume after a restart, but the service log is read over shoulders and
attached to bug reports, so it must not.
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

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
    return " ".join(redact_url(token) for token in text.split(" "))


#: The agent navigates wherever it is told, so the link itself is a boundary: an
#: arbitrary URL would turn the recorder into a probe for local services.
_PRIVATE_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa")


def _is_loopback(host: str) -> bool:
    if host in ("localhost", "::1") or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_private(host: str) -> bool:
    if host.endswith(_PRIVATE_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_link_local or address.is_reserved or address.is_unspecified


def allow_private_hosts() -> bool:
    """Opt-in for a self-hosted meeting server on the corporate network."""
    return os.environ.get("TRANSCRIBE_ALLOW_PRIVATE_HOSTS", "").strip().lower() in {"1", "true", "yes", "on"}


def validate_meeting_url(value: str) -> str:
    """Reject anything that is not a plausible meeting link.

    Rules, from the cheap ones to the expensive:

    * ``http``/``https`` with a host, and no credentials in the URL;
    * ``http`` is allowed only for loopback, because that is where the local test
      double lives — everything else must be https;
    * literal private, link-local and reserved addresses are refused unless the
      operator opted in: ``169.254.169.254`` is the classic cloud metadata probe.

    A DNS name can still resolve to a private address; closing that properly needs
    an egress proxy, which is out of scope for a local recorder.
    """
    raw = value.strip()
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("нужна полная ссылка вида https://…")
    if parsed.username or parsed.password:
        raise ValueError("уберите логин и пароль из ссылки")

    host = (parsed.hostname or "").lower()
    loopback = _is_loopback(host)
    if parsed.scheme == "http" and not loopback and not allow_private_hosts():
        raise ValueError("http допустим только для локальной встречи, для остальных нужен https")
    if _is_private(host) and not (loopback or allow_private_hosts()):
        raise ValueError(
            "ссылка ведёт на внутренний адрес; если это ваша встреча, включите TRANSCRIBE_ALLOW_PRIVATE_HOSTS=1"
        )
    return raw
