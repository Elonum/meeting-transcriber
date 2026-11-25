"""Browser capture agent: join a meeting, take each participant's media.

Why the browser
---------------
Jazz (like most VCS products) has no public bot API. The web client, however,
receives one ``RTCPeerConnection`` track per remote participant. Hooking that
constructor before page scripts run gives us, for every participant:

* audio — already demultiplexed, so the speaker *is* the track (no diarization);
* video — frames for face detection at native resolution;
* identity — the display name from the participant tile in the DOM.

How audio gets out
------------------
An ``AudioContext({sampleRate: 16000})`` resamples each track to the rate Whisper
wants, a ``ScriptProcessor`` node accumulates frames, and 100 ms packets go to
Python through a Playwright binding (base64, JSON). No ffmpeg, no temp files, no
decoding step — PCM int16 straight into the ASR buffer.

Limitations, stated honestly
----------------------------
* ``ScriptProcessor`` is deprecated, but it is available everywhere and needs no
  bundling; ``AudioWorklet`` requires a separate module file served by the page,
  which we cannot inject into a third-party client.
* The join flow is heuristic: fill a name field, click a button that looks like
  "join". Jazz may use SSO, a lobby or a captcha, which is exactly what
  ``spike/probe_jazz.py`` is for.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AgentUnavailableError, describe_error
from .security import redact_url

log = logging.getLogger(__name__)

#: Enough of a browser to be indistinguishable from a human, plus synthetic media
#: so no hardware is needed. ``--enable-automation`` is removed at launch time
#: (see ``ignore_default_args``) because the SaluteJazz client checks for it.
#:
#: ``--use-fake-device-for-media-stream`` on its own makes Chrome synthesize a
#: beeping microphone signal, which the other participants hear as a drum. The
#: agent has to unmute to make the SFU forward remote tracks, so it publishes a
#: silent WAV instead: see :func:`ensure_silence_wav`.
LAUNCH_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-blink-features=AutomationControlled",
]

#: Name a track gets before the room tells us who it is.
UNNAMED_PREFIX = "Участник "

#: ``Участник 2`` is ours; ``Участник проекта`` is somebody's real name.
_UNNAMED_RE = re.compile(r"^Участник\s+\d+$")


def is_placeholder(name: str | None) -> bool:
    """True when the name is our own fallback, not something the product showed."""
    return not name or bool(_UNNAMED_RE.match(name.strip()))


def ensure_silence_wav(seconds: float = 1.0, rate: int = 48_000) -> Path:
    """A silent WAV for ``--use-file-for-fake-audio-capture``, created once.

    Chrome loops that file as the microphone input, so the agent publishes audio
    (required for the SFU to send us the other tracks) without making a sound.
    """
    candidates = [Path(tempfile.gettempdir()) / "transcribe-silence.wav"]
    candidates.append(Path(__file__).resolve().parents[2] / ".tmp" / "transcribe-silence.wav")
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 44:
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes(b"\x00\x00" * int(rate * seconds))
            return path
        except OSError:
            continue
    raise RuntimeError("cannot create a silent audio file for the fake microphone")


def launch_args() -> list[str]:
    """Browser flags, including the silent file that replaces the beeping mic."""
    args = list(LAUNCH_ARGS)
    try:
        args.append(f"--use-file-for-fake-audio-capture={ensure_silence_wav()}")
    except RuntimeError:
        log.warning("agent: no silent audio file, the microphone may beep")
    return args

#: Runs before any page script: hides the automation marker and pre-sets the web
#: client's own bypass flag. Without both, SaluteJazz shows "браузер не
#: поддерживается" and only offers to open the desktop app.
PRELUDE_JS = """
try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
try { sessionStorage.setItem('s2b.browserChecker.skipChecking', 'true'); } catch (e) {}
"""

#: Invite links carry the password encrypted in ``?psw=``; that form is a deep
#: link for the desktop app, while the web lobby is the same path without it.
DEEP_LINK_PARAMS = ("psw",)


def strip_deep_link_params(url: str) -> tuple[str, list[str]]:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    dropped = [key for key, _value in query if key in DEEP_LINK_PARAMS]
    if not dropped:
        return url, []
    kept = [(key, value) for key, value in query if key not in DEEP_LINK_PARAMS]
    return urlunsplit(parts._replace(query=urlencode(kept))), dropped

CAPTURE_JS = r"""
(() => {
  if (window.__transcribeAgent) return;

  const SEND_EVERY_MS = 100;
  const state = { tracks: new Map(), contexts: new Map(), started: false, errors: [] };
  window.__transcribeAgent = state;

  const send = (key, payload) => {
    try { window.__submitAudio(key, payload); } catch (e) { state.errors.push(String(e)); }
  };

  const ensureMonitor = (track, key) => {
    if (state.contexts.has(key)) return state.contexts.get(key);
    if (track.kind !== 'audio' || !track.getSettings) return null;
    let context;
    try {
      // The browser resamples into this rate, so the server never has to.
      context = new AudioContext({ sampleRate: 16000 });
    } catch (e) {
      try { context = new AudioContext(); } catch (e2) { state.errors.push('AudioContext: ' + e2); return null; }
    }
    const stream = new MediaStream([track]);
    const source = context.createMediaStreamSource(stream);
    const processor = context.createScriptProcessor(2048, 1, 1);
    const silent = context.createGain();
    silent.gain.value = 0;

    let pending = [];
    let pendingSamples = 0;
    const flush = () => {
      if (!pendingSamples) return;
      const merged = new Float32Array(pendingSamples);
      let offset = 0;
      for (const chunk of pending) { merged.set(chunk, offset); offset += chunk.length; }
      pending = [];
      pendingSamples = 0;
      const pcm = new Int16Array(merged.length);
      for (let i = 0; i < merged.length; i++) {
        const v = Math.max(-1, Math.min(1, merged[i]));
        pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
      }
      const bytes = new Uint8Array(pcm.buffer);
      let binary = '';
      const step = 0x8000;
      for (let i = 0; i < bytes.length; i += step) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
      }
      send(key, btoa(binary));
    };

    processor.onaudioprocess = (event) => {
      const input = event.inputBuffer.getChannelData(0);
      const copy = new Float32Array(input.length);
      copy.set(input);
      pending.push(copy);
      pendingSamples += copy.length;
      if (pendingSamples >= context.sampleRate * SEND_EVERY_MS / 1000) flush();
    };

    source.connect(processor);
    processor.connect(silent);
    silent.connect(context.destination);
    if (context.state === 'suspended') context.resume().catch(() => {});

    const handle = { context, source, processor, key, sampleRate: context.sampleRate, flush };
    state.contexts.set(key, handle);
    return handle;
  };

  const nameForElement = (element) => {
    const candidates = [
      element.getAttribute && element.getAttribute('aria-label'),
      element.getAttribute && element.getAttribute('data-name'),
      element.getAttribute && element.getAttribute('title'),
    ];
    const tile = element.closest && element.closest('[class*="participant"], [class*="tile"], [class*="user"], [data-participant]');
    if (tile) {
      candidates.push(tile.getAttribute('aria-label'), tile.getAttribute('data-name'));
      const labelled = tile.querySelector('[class*="name"], [data-name]');
      if (labelled) candidates.push(labelled.getAttribute('aria-label'), labelled.textContent);
      candidates.push((tile.textContent || '').trim());
    }
    for (const value of candidates) {
      if (typeof value === 'string') {
        const clean = value.split('\n')[0].trim();
        if (clean && clean.length <= 60) return clean;
      }
    }
    return null;
  };

  const attach = (track, key, element) => {
    if (!track || track.kind !== 'audio') return;
    if (state.tracks.has(key)) return;
    const entry = { key, kind: track.kind, label: track.label, name: null, videoTrackId: null, startedAt: Date.now() };
    if (element) {
      entry.name = nameForElement(element);
      // Face snapshots are taken from the participant's video track, so keep the
      // id of the video element's track alongside the audio one.
      try {
        const stream = element.srcObject;
        if (stream && stream.getVideoTracks) {
          const video = stream.getVideoTracks()[0];
          if (video) entry.videoTrackId = video.id;
        }
      } catch (e) {}
    }
    state.tracks.set(key, entry);
    try { ensureMonitor(track, key); } catch (e) { state.errors.push(String(e)); }
    window.__submitTrack(JSON.stringify(entry));
  };

  const originalPC = window.RTCPeerConnection;
  if (originalPC) {
    window.RTCPeerConnection = function (...args) {
      const pc = new originalPC(...args);
      const endTrack = (track) => {
        if (!track) return;
        try { window.__trackEnded(track.id); } catch (e) { state.errors.push(String(e)); }
      };
      // One listener per connection, not per track.
      pc.addEventListener('connectionstatechange', () => {
        const connection = pc.connectionState;
        if (connection === 'failed' || connection === 'closed' || connection === 'disconnected') {
          try { pc.getReceivers().forEach((receiver) => endTrack(receiver.track)); } catch (e) {}
        }
      });
      pc.addEventListener('track', (event) => {
        const track = event.track;
        if (!track) return;
        // A remote track that ends (the person hung up) is the clearest signal
        // that they left; the participant list lags by a few seconds.
        track.addEventListener('ended', () => endTrack(track));
        const streams = event.streams || [];
        const key = track.id;
        const element = Array.from(document.querySelectorAll('video, audio')).find((node) => {
          const stream = node.srcObject;
          return stream && stream.getTracks && stream.getTracks().some((t) => t.id === track.id);
        });
        const describe = (node) => attach(track, key, node);
        if (element) describe(element);
        else setTimeout(() => {
          const found = Array.from(document.querySelectorAll('video, audio')).find((node) => {
            const stream = node.srcObject;
            return stream && stream.getTracks && stream.getTracks().some((t) => t.id === track.id);
          });
          describe(found || null);
        }, 1200);
        void streams;
      });
      return pc;
    };
    window.RTCPeerConnection.prototype = originalPC.prototype;
  }

  window.__probeAgentStop = () => {
    state.contexts.forEach((handle) => {
      try { handle.flush(); handle.processor.disconnect(); handle.source.disconnect(); handle.context.close(); } catch (e) {}
    });
    state.contexts.clear();
  };

  // Video snapshots: the pipeline asks for a frame when it needs a face.
  // Resolution goes by the *audio* track id, because the page may attach the
  // video track to the same stream seconds later, when the person turns the
  // camera on.
  const videoElementFor = (key) => {
    const entry = state.tracks.get(key);
    const videoId = entry && entry.videoTrackId;
    const videos = () => Array.from(document.querySelectorAll('video'));
    if (videoId) {
      const node = videos().find((video) => {
        const stream = video.srcObject;
        return stream && stream.getVideoTracks && stream.getVideoTracks().some((t) => t.id === videoId);
      });
      if (node) return node;
    }
    const node = videos().find((video) => {
      const stream = video.srcObject;
      if (!stream || !stream.getTracks) return false;
      return stream.getTracks().some((t) => t.id === key);
    });
    if (node && entry) {
      try {
        const videoTrack = node.srcObject.getVideoTracks()[0];
        if (videoTrack) entry.videoTrackId = videoTrack.id;
      } catch (e) {}
    }
    return node || null;
  };

  window.__grabFaceForAudio = (key, maxWidth) => {
    const node = videoElementFor(key);
    if (!node || !node.videoWidth) return null;
    const scale = Math.min(1, (maxWidth || 320) / node.videoWidth);
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(node.videoWidth * scale);
    canvas.height = Math.round(node.videoHeight * scale);
    const context = canvas.getContext('2d');
    context.drawImage(node, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/jpeg', 0.75);
  };

  window.__grabFace = (trackId, maxWidth) => {
    const node = Array.from(document.querySelectorAll('video')).find((video) => {
      const stream = video.srcObject;
      return stream && stream.getVideoTracks && stream.getVideoTracks().some((t) => t.id === trackId);
    });
    if (!node || !node.videoWidth) return null;
    const scale = Math.min(1, (maxWidth || 320) / node.videoWidth);
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(node.videoWidth * scale);
    canvas.height = Math.round(node.videoHeight * scale);
    const context = canvas.getContext('2d');
    context.drawImage(node, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/jpeg', 0.75);
  };
})();
"""

NAME_SELECTORS = [
    'input[name="displayName"]',
    'input[placeholder*="имя" i]',
    'input[placeholder*="name" i]',
    'input[name="name"]',
    'input[autocomplete="name"]',
]

JOIN_SELECTORS = [
    'button:has-text("Подключиться")',
    'button:has-text("Присоединиться")',
    'button:has-text("Войти в комнату")',
    'button:has-text("Войти")',
    'button:has-text("Join")',
    'button:has-text("Enter")',
    'button:has-text("Продолжить")',
]

PASSWORD_SELECTORS = [
    'input[name="password"]',
    'input[type="password"]',
    'input[placeholder*="ароль" i]',
    'input[placeholder*="password" i]',
]

#: SaluteJazz: aria-label toggles between "Включить звук" and "Выключить звук".
MIC_SELECTORS = ['[data-testid="microphone"]', '[aria-label*="Включить звук"]']

#: Shown when the client decides the link belongs to its desktop app; the second
#: button is the product's own "continue anyway" escape hatch.
BLOCKER_SELECTORS = [
    'button:has-text("Продолжить в браузере")',
    'button:has-text("Все равно продолжить")',
    'button:has-text("Всё равно продолжить")',
    'button:has-text("Продолжить")',
    'button:has-text("Пропустить")',
    'button:has-text("Skip")',
]


#: Console noise that says nothing about the join and only clutters the UI.
#: All of these come from the third-party client, not from the agent.
CONSOLE_NOISE = (
    "ScriptProcessorNode is deprecated",
    "Wake Lock permission",
    "Trying to init when instance already exists",
    "Permissions policy violation: unload",
    "Failed to load resource: the server responded with a status of 404",
)


def is_console_noise(text: str) -> bool:
    return any(marker in text for marker in CONSOLE_NOISE)


@dataclass(slots=True)
class AudioChunk:
    track_id: str
    participant_name: str
    pcm: bytes
    received_ms: int


@dataclass(slots=True)
class TrackInfo:
    track_id: str
    name: str
    label: str
    video_track_id: str | None = None
    present: bool = True


@dataclass(slots=True)
class ParticipantUpdate:
    """A change in who is in the room, as far as a browser can know it."""

    track_id: str
    name: str | None = None
    present: bool | None = None


class BrowserAgent:
    """Owns one browser page joined to one meeting."""

    def __init__(
        self,
        url: str,
        *,
        display_name: str = "Ассистент (транскрибация)",
        headless: bool = True,
        channel: str | None = "chrome",
        join_timeout_ms: int = 45_000,
        password: str | None = None,
        strip_invite: bool = False,
    ) -> None:
        # An invite link with ?psw= carries the password. Keep it by default: the
        # web lobby accepts it once the browser check is bypassed, and the plain
        # password never has to be typed. Stripping is a fallback for clients
        # that insist on the app deep link.
        self.url = url
        self.dropped_params: list[str] = []
        if strip_invite:
            self.url, self.dropped_params = strip_deep_link_params(url)
            if self.dropped_params:
                log.info("agent: dropped %s from the meeting URL", self.dropped_params)
        self.password = password
        self.display_name = display_name
        self.headless = headless
        self.channel = channel
        self.join_timeout_ms = join_timeout_ms

        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self.page: Any = None
        self._queue: asyncio.Queue[AudioChunk] = asyncio.Queue(maxsize=2048)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tracks: dict[str, TrackInfo] = {}
        self._remote_names: list[str] = []
        self.track_events: asyncio.Queue[TrackInfo] = asyncio.Queue()
        #: Who joined, got renamed or left while the meeting runs.
        self.updates: asyncio.Queue[ParticipantUpdate] = asyncio.Queue()
        #: True while at least one other person is in the room.
        self.human_present = False
        #: A participant list can flicker while the room re-renders, so a name has
        #: to be missing this many polls in a row before we call it a departure.
        self.absence_strikes = 2
        self._strikes: dict[str, int] = {}
        self._sync_task: asyncio.Task | None = None
        self.notes: list[str] = []
        self.crashed = False
        self.crash_reason: str | None = None

    def _mark_crashed(self, reason: str) -> None:
        """A closed or crashed page can never produce audio again."""
        if self.crashed:
            return
        self.crashed = True
        self.crash_reason = reason
        self.notes.append(f"agent: {reason}")
        log.warning("agent: %s", reason)

    def is_alive(self) -> bool:
        if self.crashed:
            return False
        page = self.page
        return page is not None and not page.is_closed()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> TrackInfo | None:
        from playwright.async_api import async_playwright

        self._loop = asyncio.get_running_loop()
        try:
            self._playwright = await async_playwright().start()
        except NotImplementedError as error:
            # Playwright spawns its driver as a subprocess. uvicorn --reload on
            # Windows installs a selector loop, which cannot do that, and the raw
            # error text is empty — that produced a bare "Ошибка: ." in the UI.
            raise AgentUnavailableError(
                "браузерный агент не может запуститься: сервер работает на SelectorEventLoop, "
                "а он в Windows не умеет запускать подпроцессы. Так бывает при uvicorn --reload. "
                "Запустите сервер без него (scripts\\dev.cmd) и повторите."
            ) from error
        except Exception as error:  # noqa: BLE001 - the driver may be missing entirely
            raise AgentUnavailableError(
                f"браузерный агент не может запуститься: {describe_error(error)}. Проверьте установку: "
                ".venv\\Scripts\\python.exe -m playwright install chrome"
            ) from error

        launch_flags = launch_args()
        channels = [self.channel] if self.channel else [None]
        last_error: Exception | None = None
        for channel in channels + [None]:
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless,
                    channel=channel,
                    args=launch_flags,
                    # Playwright adds this by default and VCS clients detect it.
                    ignore_default_args=["--enable-automation"],
                )
                log.info("agent: browser launched (%s)", channel or "chromium")
                break
            except Exception as error:  # noqa: BLE001 - try the next channel
                last_error = error
        if self._browser is None:
            raise RuntimeError(f"cannot launch a browser: {last_error}")

        self._context = await self._browser.new_context(
            permissions=["microphone", "camera"],
            viewport={"width": 1440, "height": 900},
            locale="ru-RU",
        )
        # Must be awaited: an un-awaited coroutine silently skips the scripts,
        # which is exactly how the browser check slipped through once.
        await self._context.add_init_script(PRELUDE_JS)
        self.page = await self._context.new_page()
        self.page.on("pageerror", lambda error: self.notes.append(f"pageerror: {error}"))
        self.page.on("crash", lambda *_: self._mark_crashed("page crashed"))
        self.page.on("close", lambda *_: self._mark_crashed("page closed"))
        self.page.on("console", self._on_console)

        await self.page.expose_binding("__submitAudio", self._on_audio)
        await self.page.expose_binding("__submitTrack", self._on_track)
        await self.page.expose_binding("__trackEnded", self._on_track_ended)
        await self.page.add_init_script(CAPTURE_JS)

        log.info("agent: joining %s", redact_url(self.url))
        await self.page.goto(self.url, wait_until="domcontentloaded", timeout=self.join_timeout_ms)
        await self._join()
        # People join and leave after us; the room's participant list is the only
        # source of truth a browser has, so watch it while the meeting runs.
        self._sync_task = asyncio.create_task(self._sync_loop())

        try:
            await asyncio.wait_for(self.track_events.get(), timeout=20.0)
        except TimeoutError:
            self.notes.append("no tracks appeared within 20 s")
        return next(iter(self._tracks.values()), None)

    async def _join(self) -> None:
        """Guest lobby: display name, meeting password, connect, then unmute.

        Verified against the SaluteJazz web client. Two things make this loop
        instead of a straight sequence of clicks:

        * the client sometimes shows "откройте в приложении" (it fires a
          ``jazz://join`` deep link) even with the bypass flag set, and offers a
          "continue in the browser" button — it has to be pressed;
        * the lobby paints a few seconds after load.

        Nothing is published to the SFU while everybody is muted, so the agent
        unmutes itself once it is inside — that is what makes remote tracks
        appear at all.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 45
        room_ready = False

        while loop.time() < deadline:
            # Either the microphone control or the participant list means we are
            # inside: the product moves these controls around between releases.
            if await self.page.locator('[data-testid="microphone"]').count() or await self.page.locator(
                '[data-testid^="participantItem-"]'
            ).count():
                room_ready = True
                break

            await self._dismiss_blockers()

            filled = False
            for selector in NAME_SELECTORS:
                locator = self.page.locator(selector).first
                try:
                    if await locator.count() and await locator.is_visible():
                        await locator.fill(self.display_name)
                        self.notes.append(f"filled name via {selector}")
                        filled = True
                        break
                except Exception:  # noqa: BLE001 - heuristics only
                    continue

            if self.password:
                for selector in PASSWORD_SELECTORS:
                    locator = self.page.locator(selector).first
                    try:
                        if await locator.count() and await locator.is_visible():
                            await locator.fill(self.password)
                            self.notes.append(f"filled password via {selector}")
                            break
                    except Exception:  # noqa: BLE001
                        continue

            if filled:
                for selector in JOIN_SELECTORS:
                    locator = self.page.locator(selector).first
                    try:
                        if await locator.count() and await locator.is_visible():
                            await locator.click()
                            self.notes.append(f"clicked {selector}")
                            break
                    except Exception:  # noqa: BLE001
                        continue

            await self.page.wait_for_timeout(1200)

        if not room_ready:
            self.notes.append("room UI did not appear")
            return

        self.notes.append("entered the room")
        await self.page.wait_for_timeout(1200)
        await self.sync_participants()
        await self._unmute()
        await self.page.wait_for_timeout(800)
        if not self._remote_names:
            await self.sync_participants()

    async def _dismiss_blockers(self) -> None:
        """Press "continue in browser" if the client insists on its desktop app."""
        for selector in BLOCKER_SELECTORS:
            locator = self.page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    await locator.click()
                    self.notes.append(f"dismissed blocker via {selector}")
                    await self.page.wait_for_timeout(1200)
                    return
            except Exception:  # noqa: BLE001
                continue

    async def _unmute(self) -> None:
        """Publish our own audio: the SFU stays idle while everyone is muted."""
        for selector in MIC_SELECTORS:
            locator = self.page.locator(selector).first
            try:
                aria = await locator.get_attribute("aria-label") or ""
                if await locator.count() and await locator.is_visible():
                    if "Включить" in aria:
                        await locator.click()
                        self.notes.append("unmuted")
                    return
            except Exception:  # noqa: BLE001
                continue

    async def _read_participant_names(self) -> list[str]:
        """Participant names come from the tile's aria-label, minus our own tile."""
        try:
            names = await self.page.evaluate(
                """
                () => Array.from(document.querySelectorAll('[data-testid^="participantItem-"]'))
                  .map((node) => {
                    const aria = node.getAttribute('aria-label') || '';
                    const self = aria.includes('Это вы');
                    return { name: aria.split('.')[0].trim(), self };
                  })
                  .filter((item) => item.name && !item.self)
                  .map((item) => item.name)
                """
            )
        except Exception:  # noqa: BLE001 - the room UI is not always present
            return []
        if not isinstance(names, list):
            return []
        # Never label a remote track with our own display name.
        return [str(name) for name in names if name and str(name) != self.display_name]

    def sync_names(self, names: list[str]) -> list[ParticipantUpdate]:
        """Reconcile the room's list with our tracks. Pure, so it can be tested.

        Two jobs, both needed because the room changes after the join:

        * a track that arrived before its tile was painted carries our fallback
          name — give it the real one as soon as the room lists it;
        * a name that disappears from the room means the person left. Their track
          often stays alive at the SFU, so the DOM is the only signal.
        """
        clean = [name for name in names if name and name != self.display_name]
        self._remote_names = clean
        self.human_present = bool(clean)

        updates: list[ParticipantUpdate] = []

        used = {info.name for info in self._tracks.values() if not is_placeholder(info.name)}
        for info in self._tracks.values():
            if not is_placeholder(info.name):
                continue
            candidate = next((name for name in clean if name not in used), None)
            if candidate is None:
                break
            info.name = candidate
            info.present = True
            used.add(candidate)
            updates.append(ParticipantUpdate(track_id=info.track_id, name=candidate, present=True))

        listed = set(clean)
        for info in self._tracks.values():
            if is_placeholder(info.name):
                continue
            if info.name in listed:
                self._strikes.pop(info.track_id, None)
                if not info.present:
                    info.present = True
                    updates.append(ParticipantUpdate(track_id=info.track_id, present=True))
                continue
            if not info.present:
                continue
            strikes = self._strikes.get(info.track_id, 0) + 1
            self._strikes[info.track_id] = strikes
            if strikes >= self.absence_strikes:
                info.present = False
                updates.append(ParticipantUpdate(track_id=info.track_id, present=False))

        return updates

    async def sync_participants(self) -> list[ParticipantUpdate]:
        """Read the room and publish what changed."""
        updates = self.sync_names(await self._read_participant_names())
        for update in updates:
            self._safe_put(self.updates, update)
            log.info(
                "agent: %s %s",
                "left" if update.present is False else "updated",
                self._tracks[update.track_id].name if update.track_id in self._tracks else update.track_id,
            )
        return updates

    async def _sync_loop(self, interval_s: float = 3.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.sync_participants()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a broken page must not kill the session
                log.debug("agent: participant sync failed", exc_info=True)

    async def close(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sync_task
            self._sync_task = None
        try:
            if self.page is not None:
                await self.page.evaluate("window.__probeAgentStop && window.__probeAgentStop()")
        except Exception:  # noqa: BLE001
            pass
        for resource in (self._context, self._browser):
            with contextlib.suppress(Exception):
                if resource is not None:
                    await resource.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()

    def _on_console(self, message: Any) -> None:
        """Keep join diagnostics, drop the warnings every browser prints."""
        if message.type not in ("error", "warning"):
            return
        text = f"console.{message.type}: {message.text}"
        if is_console_noise(message.text):
            log.debug("agent: %s", text)
            return
        self.notes.append(text)

    # -- callbacks from the page --------------------------------------------

    def _on_audio(self, source: Any, track_id: str, payload: str) -> None:
        del source
        try:
            pcm = base64.b64decode(payload)
        except Exception:  # noqa: BLE001
            return
        info = self._tracks.get(track_id)
        chunk = AudioChunk(
            track_id=track_id,
            participant_name=info.name if info else "Спикер",
            pcm=pcm,
            received_ms=0,
        )
        self._push_chunk(chunk)

    def _on_track(self, source: Any, payload: str) -> None:
        del source
        try:
            data = json.loads(payload)
        except Exception:  # noqa: BLE001
            return
        track_id = str(data.get("key", ""))
        if not track_id or track_id in self._tracks:
            return
        name = (data.get("name") or "").strip()
        if not name or len(name) > 60:
            # Fall back to the participant list from the room UI, in join order.
            used = {info.name for info in self._tracks.values()}
            remaining = [item for item in self._remote_names if item not in used]
            name = remaining[0] if remaining else f"{UNNAMED_PREFIX}{len(self._tracks) + 1}"
        info = TrackInfo(
            track_id=track_id,
            name=name,
            label=str(data.get("label") or ""),
            video_track_id=(str(data["videoTrackId"]) if data.get("videoTrackId") else None),
        )
        self._tracks[track_id] = info
        log.info("agent: track %s -> %s", track_id[:8], name)
        self._safe_put(self.track_events, info)

    def _on_track_ended(self, source: Any, track_id: str) -> None:
        """A closed track is a clear "left" signal, earlier than the DOM list."""
        del source
        info = self._tracks.get(str(track_id))
        if info is None or not info.present:
            return
        info.present = False
        log.info("agent: track %s ended (%s)", info.track_id[:8], info.name)
        self._safe_put(self.updates, ParticipantUpdate(track_id=info.track_id, present=False))

    def _push_chunk(self, chunk: AudioChunk) -> None:
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(chunk)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def _safe_put(self, queue: asyncio.Queue, item: Any) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(item)

    # -- consumption ---------------------------------------------------------

    async def chunks(self) -> Any:
        while True:
            yield await self._queue.get()

    async def grab_face(self, track_id: str, max_width: int = 320) -> str | None:
        """A JPEG frame of the participant behind this audio track, if any.

        The key is the audio track id on purpose: a camera switched on later
        joins the same stream, and the video id we saw at join time would be
        stale by then.
        """
        try:
            return await self.page.evaluate(
                "([id, width]) => window.__grabFaceForAudio ? window.__grabFaceForAudio(id, width) : null",
                [track_id, max_width],
            )
        except Exception:  # noqa: BLE001
            return None

    async def screenshot(self, path: str) -> None:
        await self.page.screenshot(path=path)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "agent"
