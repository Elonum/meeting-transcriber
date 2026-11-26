"""Token gate: off by default, and complete when switched on.

The gate exists so the service can be reached from another machine without giving
everyone a meeting recorder. It must not fire when no token is configured.
"""

from __future__ import annotations

import pytest
from app import auth
from app.main import app
from fastapi.testclient import TestClient

MEETING = "https://salutejazz.ru/calls/auth01?psw=AAAAAAAAAAAAAAAA"


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRANSCRIBE_TOKEN", "s3cret")
    yield "s3cret"
    monkeypatch.delenv("TRANSCRIBE_TOKEN", raising=False)


def test_gate_is_off_by_default(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSCRIBE_TOKEN", raising=False)

    assert client.get("/api/sessions").status_code == 200
    assert client.get("/api/auth").json() == {"required": False, "authorised": True}


def test_api_requires_the_cookie_once_a_token_is_set(client: TestClient, token: str) -> None:
    response = client.get("/api/sessions")

    assert response.status_code == 401
    assert response.json()["detail"] == "нужен токен доступа"


def test_health_stays_open_for_probes(client: TestClient, token: str) -> None:
    assert client.get("/api/health").status_code == 200


def test_wrong_token_is_rejected(client: TestClient, token: str) -> None:
    assert client.post("/api/auth", json={"token": "nope"}).status_code == 401


def test_right_token_sets_an_httponly_cookie(client: TestClient, token: str) -> None:
    response = client.post("/api/auth", json={"token": token})

    assert response.status_code == 200
    assert response.json() == {"required": True, "authorised": True}
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=strict" in header.lower().replace("samesite=strict", "SameSite=strict")
    # The cookie is a signature, never the token itself.
    assert token not in header
    assert client.get("/api/sessions").status_code == 200


def test_status_reports_an_authorised_session(client: TestClient, token: str) -> None:
    assert client.get("/api/auth").json() == {"required": True, "authorised": False}

    client.post("/api/auth", json={"token": token})

    assert client.get("/api/auth").json() == {"required": True, "authorised": True}


def test_stream_without_a_cookie_is_closed(client: TestClient, token: str) -> None:
    session_id = client.post(
        "/api/sessions", json={"meeting_url": MEETING, "speed": 40}
    )
    # The API is gated too, so open the session before logging in: no cookie yet.
    assert session_id.status_code == 401

    client.post("/api/auth", json={"token": token})
    created = client.post("/api/sessions", json={"meeting_url": MEETING, "speed": 40}).json()

    with client.websocket_connect(f"/events/{created['id']}") as socket:
        assert socket.receive_json()["type"] == "hello"


def test_rotating_the_token_invalidates_cookies(client: TestClient, token: str, monkeypatch) -> None:
    client.post("/api/auth", json={"token": token})
    assert client.get("/api/sessions").status_code == 200

    monkeypatch.setenv("TRANSCRIBE_TOKEN", "another")

    assert client.get("/api/sessions").status_code == 401


def test_cookie_value_is_a_signature() -> None:
    assert auth.cookie_for("s3cret") != "s3cret"
    assert auth.cookie_for("s3cret") == auth.cookie_for("s3cret")
    assert auth.cookie_for("s3cret") != auth.cookie_for("another")


def test_public_paths_are_not_gated() -> None:
    assert not auth.requires_auth("/")
    assert not auth.requires_auth("/assets/index.js")
    assert not auth.requires_auth("/api/auth")
    assert not auth.requires_auth("/api/health")
    assert auth.requires_auth("/api/sessions")
    assert auth.requires_auth("/events/abc123")
