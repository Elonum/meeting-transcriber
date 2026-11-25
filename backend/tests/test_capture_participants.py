"""Participant tracking: names that arrive late, and people who leave.

The browser is not involved: ``sync_names`` is the pure half of the periodic
room check, and the silence file is a plain WAV that can be parsed back.
"""

from __future__ import annotations

import wave

from app.capture import (
    UNNAMED_PREFIX,
    BrowserAgent,
    TrackInfo,
    ensure_silence_wav,
    is_placeholder,
    launch_args,
)


def agent_with_tracks(*names: str) -> BrowserAgent:
    agent = BrowserAgent("https://salutejazz.ru/calls/abc123", display_name="Ассистент (бот)")
    for index, name in enumerate(names, start=1):
        track_id = f"track-{index}"
        agent._tracks[track_id] = TrackInfo(track_id=track_id, name=name, label="audio")
    return agent


def test_placeholder_detection() -> None:
    assert is_placeholder(None)
    assert is_placeholder("")
    assert is_placeholder(f"{UNNAMED_PREFIX}2")
    assert not is_placeholder("Адель")
    assert not is_placeholder("Участник проекта")  # a real name that starts similarly


def test_late_joiner_gets_the_real_name() -> None:
    """The phone guest joined after the list was read; the name must still land."""
    agent = agent_with_tracks(f"{UNNAMED_PREFIX}1")

    updates = agent.sync_names(["Адель"])

    assert agent._tracks["track-1"].name == "Адель"
    assert [(u.track_id, u.name, u.present) for u in updates] == [("track-1", "Адель", True)]
    assert agent.human_present is True


def test_rename_is_reported_once() -> None:
    agent = agent_with_tracks(f"{UNNAMED_PREFIX}1")

    agent.sync_names(["Адель"])
    second = agent.sync_names(["Адель"])

    assert second == [], "an unchanged room must not produce events"
    assert agent._tracks["track-1"].name == "Адель"


def test_departure_marks_the_participant_absent() -> None:
    agent = agent_with_tracks("Роман")
    agent.sync_names(["Роман"])

    first = agent.sync_names([])
    second = agent.sync_names([])

    assert first == [], "a single missing poll is a re-render, not a departure"
    assert [(u.track_id, u.present) for u in second] == [("track-1", False)]
    assert agent._tracks["track-1"].present is False
    assert agent.human_present is False
    # Repeated polls must not spam the transcript.
    assert agent.sync_names([]) == []


def test_presence_returns_after_a_reconnect() -> None:
    agent = agent_with_tracks("Роман")
    agent.sync_names(["Роман"])
    agent.sync_names([])
    agent.sync_names([])

    updates = agent.sync_names(["Роман"])

    assert [(u.track_id, u.present) for u in updates] == [("track-1", True)]


def test_flicker_does_not_disconnect() -> None:
    """One empty poll between two full ones is a repaint, not a departure."""
    agent = agent_with_tracks("Роман")
    agent.sync_names(["Роман"])

    assert agent.sync_names([]) == []
    assert agent.sync_names(["Роман"]) == []
    assert agent._tracks["track-1"].present is True


def test_two_people_two_tracks() -> None:
    agent = agent_with_tracks(f"{UNNAMED_PREFIX}1", f"{UNNAMED_PREFIX}2")

    updates = agent.sync_names(["Роман", "Адель"])

    assert [agent._tracks[f"track-{i}"].name for i in (1, 2)] == ["Роман", "Адель"]
    assert len(updates) == 2


def test_own_name_is_never_used() -> None:
    agent = agent_with_tracks("Роман")
    agent.sync_names(["Роман"])

    agent.sync_names(["Ассистент (бот)"])  # our own tile leaking through
    agent.sync_names(["Ассистент (бот)"])

    assert agent.human_present is False
    assert agent._tracks["track-1"].present is False


def test_track_end_marks_absent_once() -> None:
    agent = agent_with_tracks("Роман")
    agent._on_track_ended(None, "track-1")
    agent._on_track_ended(None, "track-1")

    assert agent.updates.qsize() == 1
    assert agent._tracks["track-1"].present is False


def test_silence_file_is_a_valid_wav() -> None:
    path = ensure_silence_wav()
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 48_000
        frames = handle.getnframes()
        payload = handle.readframes(frames)

    assert frames == 48_000  # one second
    assert payload and not any(payload), "the fake microphone must be silent"


def test_launch_flags_use_the_silent_file() -> None:
    flags = launch_args()
    assert any(flag.startswith("--use-file-for-fake-audio-capture=") for flag in flags)
    # The beeping default is only acceptable together with the silent file.
    assert "--use-fake-device-for-media-stream" in flags

def test_console_noise_is_filtered() -> None:
    from app.capture import is_console_noise

    assert is_console_noise("The ScriptProcessorNode is deprecated. Use AudioWorkletNode instead.")
    assert is_console_noise("NotAllowedError, Wake Lock permission request denied")
    assert is_console_noise("Trying to init when instance already exists. []")
    assert is_console_noise("Permissions policy violation: unload is not allowed in this document.")
    assert not is_console_noise("Failed to fetch participant list")


def test_a_page_without_a_participant_list_does_not_disconnect() -> None:
    """A test double (or a changed client) has no tiles; that is not a departure."""
    agent = agent_with_tracks("Анна Соколова")

    first = agent.sync_names([])
    second = agent.sync_names([])

    assert first == [] and second == []
    assert agent._tracks["track-1"].present is True
    assert agent.human_present is True, "presence must come from the tracks when no list exists"


def test_presence_is_inferred_from_tracks_when_no_list_exists() -> None:
    agent = agent_with_tracks("Анна Соколова")
    agent.sync_names([])
    agent._tracks["track-1"].present = False

    agent.sync_names([])

    assert agent.human_present is False


def test_departure_needs_a_list_that_was_actually_seen() -> None:
    agent = agent_with_tracks("Анна Соколова")
    agent.sync_names(["Анна Соколова"])  # the room does have a list now

    agent.sync_names([])
    updates = agent.sync_names([])

    assert [(u.track_id, u.present) for u in updates] == [("track-1", False)]
