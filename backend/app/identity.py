"""Face identity inside one meeting, without a biometric database.

The product problem: the room gives us names and nothing else, so the same person
who leaves and comes back under a slightly different name becomes a second
participant, and two people who pick one name cannot be told apart. A face
embedding answers exactly that question — "is this the same person?" — and
nothing more.

Design decisions that matter:

* **In memory only.** Embeddings are never written to the event log, the archive
  or an export: a biometric template at rest is a different legal object from a
  photo of a meeting. Stopping the session forgets everything.
* **Inside one meeting.** There is no gallery across meetings, so the agent
  cannot recognise somebody from last week. That keeps the scope narrow and the
  risk small; a cross-meeting gallery would be a separate, opt-in product.
* **Two frames must agree.** One glance at a bad frame is not proof: a merge
  needs ``confirmations`` independent observations, otherwise a single false
  positive would fuse two people's transcripts.
* **Never merge two people who are both in the room.** If the face matches an
  identity that is currently live, the honest answer is "cannot tell" — twins and
  one person on two devices both look like this.
* **A margin, not just a threshold.** The best match must beat the second best by
  ``margin``, so a face that resembles two known people is left alone.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: OpenCV's published cosine threshold for SFace (2021dec). Same person on two
#: frames lands around 0.8, different people around 0.0-0.25.
DEFAULT_THRESHOLD = 0.363
#: The best match must be this much better than the runner-up.
DEFAULT_MARGIN = 0.05
#: Independent frames that must agree before a merge happens.
DEFAULT_CONFIRMATIONS = 2
#: A tiny face carries too little detail to be recognised confidently.
MIN_FACE_PX = 80
#: YuNet score below which the frame is not trusted for identity at all.
MIN_FACE_CONFIDENCE = 0.65


def identity_enabled() -> bool:
    """Off unless the operator asks for it: biometrics are not a default."""
    return os.environ.get("TRANSCRIBE_FACE_IDENTITY", "").strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, fallback: float) -> float:
    try:
        return float(os.environ.get(name, fallback))
    except ValueError:
        log.warning("identity: %s is not a number, using %s", name, fallback)
        return fallback


def _int_env(name: str, fallback: int) -> int:
    try:
        return max(1, int(os.environ.get(name, fallback)))
    except ValueError:
        log.warning("identity: %s is not a number, using %s", name, fallback)
        return fallback


def settings() -> tuple[float, float, int]:
    """(threshold, margin, confirmations) from the environment."""
    return (
        _float_env("TRANSCRIBE_FACE_IDENTITY_THRESHOLD", DEFAULT_THRESHOLD),
        _float_env("TRANSCRIBE_FACE_IDENTITY_MARGIN", DEFAULT_MARGIN),
        _int_env("TRANSCRIBE_FACE_IDENTITY_CONFIRMATIONS", DEFAULT_CONFIRMATIONS),
    )


def normalise(vector: Sequence[float]) -> tuple[float, ...] | None:
    """Unit-length copy, so a dot product is a cosine; None for a dead vector."""
    values = tuple(float(value) for value in vector)
    norm = sum(value * value for value in values) ** 0.5
    if norm <= 1e-9:
        return None
    return tuple(value / norm for value in values)


def cosine(first: Sequence[float], second: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]; 0 for anything degenerate."""
    left = normalise(first)
    right = normalise(second)
    if left is None or right is None or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=False))


@dataclass(slots=True)
class FaceMatch:
    """A face that looks like somebody we have already seen in this meeting."""

    participant_id: str
    similarity: float
    #: True when enough frames agreed and the records may be merged.
    confirmed: bool = False


@dataclass(slots=True)
class _Entry:
    participant_id: str
    embedding: tuple[float, ...]
    name: str = ""
    present: bool = False
    samples: int = 1
    details: dict = field(default_factory=dict)


class IdentityBook:
    """Who has been in this meeting, by face. Pure logic, fully unit-tested."""

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        margin: float = DEFAULT_MARGIN,
        confirmations: int = DEFAULT_CONFIRMATIONS,
    ) -> None:
        self.threshold = threshold
        self.margin = margin
        self.confirmations = max(1, confirmations)
        self._entries: dict[str, _Entry] = {}
        #: (observer participant, candidate participant) -> frames that agreed.
        self._pending: dict[tuple[str, str], int] = {}

    # -- bookkeeping ---------------------------------------------------------

    def mark_present(self, participant_id: str, name: str | None = None) -> None:
        entry = self._entries.get(participant_id)
        if entry is None:
            return
        entry.present = True
        if name:
            entry.name = name

    def mark_absent(self, participant_id: str) -> None:
        entry = self._entries.get(participant_id)
        if entry is not None:
            entry.present = False
        self._forget_pending(participant_id)

    def rename(self, participant_id: str, name: str) -> None:
        entry = self._entries.get(participant_id)
        if entry is not None and name:
            entry.name = name

    def forget(self, participant_id: str) -> None:
        self._entries.pop(participant_id, None)
        self._forget_pending(participant_id)

    def known(self) -> dict[str, str]:
        """Participant id -> last known name, for diagnostics and tests."""
        return {entry.participant_id: entry.name for entry in self._entries.values()}

    def _forget_pending(self, participant_id: str) -> None:
        for key in [key for key in self._pending if participant_id in key]:
            del self._pending[key]

    # -- the only interesting call ------------------------------------------

    def observe(
        self,
        participant_id: str,
        embedding: Sequence[float],
        *,
        name: str | None = None,
        quality: float = 1.0,
    ) -> FaceMatch | None:
        """Feed one frame; return a match when this face is somebody we know.

        ``quality`` is the detector's confidence: a weak frame may register a new
        identity but never confirms a merge.
        """
        vector = normalise(embedding)
        if vector is None:
            return None

        own = self._entries.get(participant_id)
        if own is not None:
            if cosine(own.embedding, vector) < self.threshold:
                # A different face on a record that already has one: a bad frame,
                # or the person was replaced. Never rewrite an identity from one.
                log.debug("identity: frame for %s does not match its own record", participant_id)
                return None
            # Same face as before: a running average smooths pose and lighting
            # without ever drifting to another person.
            own.embedding = normalise(
                tuple((a * own.samples + b) / (own.samples + 1) for a, b in zip(own.embedding, vector, strict=False))
            ) or own.embedding
            own.samples += 1
            own.present = True
            if name:
                own.name = name
            # For the first few frames keep looking sideways: this may be
            # somebody we already know who left and came back under a new name.
            # Without that window the new record shadows the old one forever,
            # because it always matches itself first. The window is bounded so
            # an established participant is never re-checked against everybody.
            if own.samples <= self.confirmations + 2:
                other = self._match_other(participant_id, vector, quality)
                if other is not None:
                    return other
            return FaceMatch(participant_id=participant_id, similarity=1.0, confirmed=True)

        match = self._match_other(participant_id, vector, quality)
        if match is not None:
            return match

        # A new face: remember it so a later frame can recognise this person.
        self._entries[participant_id] = _Entry(
            participant_id=participant_id,
            embedding=vector,
            name=name or "",
            present=True,
        )
        self._forget_pending(participant_id)
        return None

    def _match_other(
        self, participant_id: str, vector: tuple[float, ...], quality: float
    ) -> FaceMatch | None:
        """The best *other* identity for this face, with the merge rules applied."""
        candidates = [
            (cosine(entry.embedding, vector), entry)
            for entry in self._entries.values()
            if entry.participant_id != participant_id
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        similarity, entry = candidates[0]
        second = candidates[1][0] if len(candidates) > 1 else 0.0
        if similarity < self.threshold or similarity - second < self.margin:
            return None

        if entry.present:
            # Both people are in the room right now. Twins, or one person on two
            # devices: merging would be a guess, so we do not.
            log.info(
                "identity: %s looks like %s (%.3f) but they are both present",
                participant_id,
                entry.participant_id,
                similarity,
            )
            self._forget_pending(participant_id)
            return None

        key = (participant_id, entry.participant_id)
        frames = self._pending.get(key, 0) + 1
        self._pending[key] = frames
        confirmed = frames >= self.confirmations and quality >= MIN_FACE_CONFIDENCE
        log.info(
            "identity: %s looks like %s (%.3f), frame %d/%d%s",
            participant_id,
            entry.participant_id,
            similarity,
            frames,
            self.confirmations,
            "" if confirmed else " (waiting)",
        )
        return FaceMatch(participant_id=entry.participant_id, similarity=similarity, confirmed=confirmed)
