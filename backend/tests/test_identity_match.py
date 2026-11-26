"""Face identity inside one meeting: the rules that keep people apart.

A false merge is much worse than a missed one — it fuses two people's words into
one transcript — so every rule here errs on the side of "cannot tell".
"""

from __future__ import annotations

import pytest
from app.identity import (
    DEFAULT_THRESHOLD,
    IdentityBook,
    cosine,
    identity_enabled,
    normalise,
    settings,
)

ALICE = (1.0, 0.0, 0.0, 0.0)
BOB = (0.0, 1.0, 0.0, 0.0)
ALICE_SLIGHTLY = (0.99, 0.05, 0.0, 0.0)


def book(**options) -> IdentityBook:
    return IdentityBook(**options)


def test_cosine_basics() -> None:
    assert cosine(ALICE, ALICE) == pytest.approx(1.0)
    assert cosine(ALICE, BOB) == pytest.approx(0.0)
    assert cosine(ALICE, (0.0, 0.0, 0.0, 0.0)) == 0.0
    assert cosine((2.0, 0.0, 0.0, 0.0), ALICE) == pytest.approx(1.0), "length must not matter"


def test_normalise_produces_unit_vectors() -> None:
    unit = normalise((3.0, 4.0))
    assert unit == pytest.approx((0.6, 0.8))
    assert normalise((0.0, 0.0)) is None


def test_a_new_face_is_remembered() -> None:
    identities = book()

    assert identities.observe("p-1", ALICE, name="Адель") is None
    assert identities.known()["p-1"] == "Адель"


def test_the_same_face_keeps_matching_its_own_record() -> None:
    identities = book()
    identities.observe("p-1", ALICE)

    match = identities.observe("p-1", ALICE_SLIGHTLY)

    assert match is not None
    assert match.participant_id == "p-1"
    assert match.confirmed


def test_a_known_record_is_not_rewritten_by_a_different_face() -> None:
    identities = book()
    identities.observe("p-1", ALICE)

    assert identities.observe("p-1", BOB) is None
    assert cosine(identities._entries["p-1"].embedding, ALICE) > 0.9


def test_one_frame_is_not_enough_to_merge() -> None:
    identities = book(confirmations=2)
    identities.observe("p-1", ALICE)
    identities.mark_absent("p-1")

    first = identities.observe("p-2", ALICE)

    assert first is not None
    assert first.participant_id == "p-1"
    assert not first.confirmed, "a single frame is a guess, not a match"


def test_two_frames_confirm_the_same_person() -> None:
    identities = book(confirmations=2)
    identities.observe("p-1", ALICE)
    identities.mark_absent("p-1")

    identities.observe("p-2", ALICE)
    second = identities.observe("p-2", ALICE_SLIGHTLY)

    assert second is not None and second.confirmed
    assert second.participant_id == "p-1"


def test_two_people_in_the_room_are_never_merged() -> None:
    """Twins and one person on two devices look exactly like this."""
    identities = book(confirmations=1)
    identities.observe("p-1", ALICE)  # present by default

    first = identities.observe("p-2", ALICE)
    second = identities.observe("p-2", ALICE)

    assert first is None, "no identity to merge into while both are present"
    assert second is None or second.participant_id == "p-2", "never into p-1"


def test_an_ambiguous_face_is_left_alone() -> None:
    identities = book(margin=0.2, confirmations=1)
    identities.observe("p-1", ALICE)
    identities.observe("p-3", BOB)
    identities.mark_absent("p-1")
    identities.mark_absent("p-3")

    blended = (0.6, 0.6, 0.0, 0.0)  # equally close to both

    assert identities.observe("p-2", blended) is None


def test_a_weak_frame_registers_but_never_confirms() -> None:
    identities = book(confirmations=1)
    identities.observe("p-1", ALICE)
    identities.mark_absent("p-1")

    match = identities.observe("p-2", ALICE, quality=0.4)

    assert match is not None and not match.confirmed


def test_a_different_face_starts_a_new_record() -> None:
    identities = book(confirmations=1)
    identities.observe("p-1", ALICE)
    identities.mark_absent("p-1")

    assert identities.observe("p-2", BOB) is None
    assert set(identities.known()) == {"p-1", "p-2"}


def test_forget_removes_the_identity_and_its_pending_frames() -> None:
    identities = book(confirmations=5)
    identities.observe("p-1", ALICE)
    identities.mark_absent("p-1")
    identities.observe("p-2", ALICE)

    identities.forget("p-1")

    assert "p-1" not in identities.known()
    # A fresh record must start counting from zero, not inherit old frames.
    match = identities.observe("p-3", ALICE)
    assert match is None, "the only remaining face is unknown again"


def test_rename_updates_the_known_name() -> None:
    identities = book()
    identities.observe("p-1", ALICE, name="Адель")

    identities.rename("p-1", "Адельы")

    assert identities.known()["p-1"] == "Адельы"


def test_identity_is_off_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSCRIBE_FACE_IDENTITY", raising=False)
    assert identity_enabled() is False
    monkeypatch.setenv("TRANSCRIBE_FACE_IDENTITY", "1")
    assert identity_enabled() is True
    monkeypatch.setenv("TRANSCRIBE_FACE_IDENTITY", "off")
    assert identity_enabled() is False


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSCRIBE_FACE_IDENTITY_THRESHOLD", raising=False)
    monkeypatch.delenv("TRANSCRIBE_FACE_IDENTITY_MARGIN", raising=False)
    monkeypatch.delenv("TRANSCRIBE_FACE_IDENTITY_CONFIRMATIONS", raising=False)

    assert settings() == (DEFAULT_THRESHOLD, 0.05, 2)

    monkeypatch.setenv("TRANSCRIBE_FACE_IDENTITY_THRESHOLD", "0.5")
    monkeypatch.setenv("TRANSCRIBE_FACE_IDENTITY_MARGIN", "0.1")
    monkeypatch.setenv("TRANSCRIBE_FACE_IDENTITY_CONFIRMATIONS", "3")
    assert settings() == (0.5, 0.1, 3)


def test_settings_survive_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBE_FACE_IDENTITY_THRESHOLD", "много")
    monkeypatch.setenv("TRANSCRIBE_FACE_IDENTITY_CONFIRMATIONS", "0")

    threshold, _margin, confirmations = settings()

    assert threshold == DEFAULT_THRESHOLD
    assert confirmations == 1, "zero confirmations would merge on a single frame"
