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
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

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
      pc.addEventListener('track', (event) => {
        const track = event.track;
        if (!track) return;
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
    'button:has-text("Присоединиться")',
    'button:has-text("Подключиться")',
    'button:has-text("Войти")',
    'button:has-text("Join")',
    'button:has-text("Enter")',
    'button:has-text("Продолжить")',
]


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


class BrowserAgent:
    """Owns one browser page joined to one meeting."""

    def __init__(
        self,
        url: str,
        *,
        display_name: str = "Ассистент (транскрибация)",
        headless: bool = True,
        channel: str | None = "msedge",
        join_timeout_ms: int = 45_000,
    ) -> None:
        self.url = url
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
        self.track_events: asyncio.Queue[TrackInfo] = asyncio.Queue()
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
        self._playwright = await async_playwright().start()

        launch_args = [
            "--use-fake-ui-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled",
        ]
        channels = [self.channel] if self.channel else [None]
        last_error: Exception | None = None
        for channel in channels + [None]:
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless, channel=channel, args=launch_args
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
        self.page = await self._context.new_page()
        self.page.on("pageerror", lambda error: self.notes.append(f"pageerror: {error}"))
        self.page.on("crash", lambda *_: self._mark_crashed("page crashed"))
        self.page.on("close", lambda *_: self._mark_crashed("page closed"))
        self.page.on(
            "console",
            lambda message: self.notes.append(f"console.{message.type}: {message.text}")
            if message.type in ("error", "warning")
            else None,
        )

        await self.page.expose_binding("__submitAudio", self._on_audio)
        await self.page.expose_binding("__submitTrack", self._on_track)
        await self.page.add_init_script(CAPTURE_JS)

        log.info("agent: joining %s", self.url)
        await self.page.goto(self.url, wait_until="domcontentloaded", timeout=self.join_timeout_ms)
        await self._join()

        try:
            await asyncio.wait_for(self.track_events.get(), timeout=20.0)
        except asyncio.TimeoutError:
            self.notes.append("no tracks appeared within 20 s")
        return next(iter(self._tracks.values()), None)

    async def _join(self) -> None:
        await self.page.wait_for_timeout(1500)
        for selector in NAME_SELECTORS:
            locator = self.page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    await locator.fill(self.display_name)
                    self.notes.append(f"filled {selector}")
                    break
            except Exception:  # noqa: BLE001 - heuristics only
                continue
        for selector in JOIN_SELECTORS:
            locator = self.page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    await locator.click()
                    self.notes.append(f"clicked {selector}")
                    break
            except Exception:  # noqa: BLE001
                continue

    async def close(self) -> None:
        try:
            if self.page is not None:
                await self.page.evaluate("window.__probeAgentStop && window.__probeAgentStop()")
        except Exception:  # noqa: BLE001
            pass
        for resource in (self._context, self._browser):
            try:
                if resource is not None:
                    await resource.close()
            except Exception:  # noqa: BLE001
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass

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
            name = f"Участник {len(self._tracks) + 1}"
        info = TrackInfo(
            track_id=track_id,
            name=name,
            label=str(data.get("label") or ""),
            video_track_id=(str(data["videoTrackId"]) if data.get("videoTrackId") else None),
        )
        self._tracks[track_id] = info
        log.info("agent: track %s -> %s", track_id[:8], name)
        self._safe_put(self.track_events, info)

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
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    # -- consumption ---------------------------------------------------------

    async def chunks(self) -> Any:
        while True:
            yield await self._queue.get()

    async def grab_face(self, track_id: str, max_width: int = 320) -> str | None:
        try:
            return await self.page.evaluate(
                "([id, width]) => window.__grabFace ? window.__grabFace(id, width) : null",
                [track_id, max_width],
            )
        except Exception:  # noqa: BLE001
            return None

    async def screenshot(self, path: str) -> None:
        await self.page.screenshot(path=path)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "agent"
