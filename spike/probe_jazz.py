"""Jazz capability probe — the day-0 spike.

It answers the four questions the whole architecture depends on:

1. Can an agent join a meeting by URL, as a guest, without SSO or lobby?
2. Does the SFU expose *per-participant* audio tracks, or one mixed track?
3. Do video tracks expose frames big enough to recognise a face?
4. Can the browser record those tracks (MediaRecorder) and hand the data out?

The probe hooks ``RTCPeerConnection`` before any page script runs, records every
remote track it sees, and writes a JSON report plus the captured chunks to disk.
Nothing is uploaded anywhere.

Usage
-----
    # first look: watch what happens, then join by hand if needed
    .venv\\Scripts\\python.exe spike\\probe_jazz.py --url "https://salutejazz.ru/<ссылка-из-приложения>" --headed

    # unattended run
    .venv\\Scripts\\python.exe spike\\probe_jazz.py --url "..." --seconds 45

Output: spike/out/<timestamp>/report.json, *.webm chunks, *.png screenshots.
Exit code is 0 when audio tracks were captured, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# Playwright's driver cannot use the system temp on this machine: use a local one.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "tools"))
from _env import ensure_local_temp as _ensure_local_temp  # noqa: E402

_ensure_local_temp()

HOOK = r"""
(() => {
  const state = {
    streams: [],
    tracks: [],
    errors: [],
    mediaRecorderSupported: typeof MediaRecorder !== 'undefined',
  };
  window.__probe = state;

  const describeTrack = (track, origin) => {
    let settings = {};
    try { settings = track.getSettings ? track.getSettings() : {}; } catch (e) {}
    return {
      id: track.id,
      kind: track.kind,
      label: track.label,
      enabled: track.enabled,
      muted: track.muted,
      readyState: track.readyState,
      settings,
      origin,
      at: Date.now(),
    };
  };

  const record = (track) => {
    if (!state.mediaRecorderSupported) return;
    if (track.kind !== 'audio') return;
    // One recorder per audio track: this is what proves (or disproves) that
    // per-participant audio is available in the browser at all.
    const key = track.id;
    if (state.__recorders && state.__recorders[key]) return;
    state.__recorders = state.__recorders || {};
    const stream = new MediaStream([track]);
    let recorder;
    try {
      recorder = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
    } catch (e) {
      state.errors.push('MediaRecorder: ' + String(e));
      return;
    }
    const chunks = [];
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size) chunks.push(event.data);
    };
    recorder.onstop = async () => {
      const blob = new Blob(chunks, { type: 'audio/webm' });
      const buffer = await blob.arrayBuffer();
      const bytes = new Uint8Array(buffer);
      let binary = '';
      const step = 0x8000;
      for (let i = 0; i < bytes.length; i += step) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
      }
      window.__probeWrite(key, btoa(binary));
    };
    recorder.start(1000);
    state.__recorders[key] = recorder;
    state.tracks.push({ ...describeTrack(track, 'recorded'), recorder: true });
  };

  const originalPC = window.RTCPeerConnection;
  if (originalPC) {
    window.RTCPeerConnection = function (...args) {
      const pc = new originalPC(...args);
      const originalAddTrack = pc.addTrack;
      pc.addTrack = function (track, ...rest) {
        state.tracks.push(describeTrack(track, 'local-addTrack'));
        return originalAddTrack.call(this, track, ...rest);
      };
      pc.addEventListener('track', (event) => {
        const stream = event.streams && event.streams[0] ? event.streams[0] : null;
        state.streams.push({
          streamId: stream ? stream.id : null,
          tracks: stream ? stream.getTracks().map((t) => describeTrack(t, 'remote-stream')) : [],
          at: Date.now(),
        });
        if (event.track) record(event.track);
      });
      return pc;
    };
    window.RTCPeerConnection.prototype = originalPC.prototype;
    Object.keys(originalPC).forEach((key) => {
      try { window.RTCPeerConnection[key] = originalPC[key]; } catch (e) {}
    });
  }

  // Some players attach srcObject directly without going through ontrack.
  const originalSrcObject = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
  if (originalSrcObject && originalSrcObject.set) {
    Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
      configurable: true,
      get: originalSrcObject.get,
      set(value) {
        if (value && typeof value.getTracks === 'function') {
          state.streams.push({
            streamId: value.id,
            source: 'srcObject',
            tracks: value.getTracks().map((t) => describeTrack(t, 'srcObject')),
            at: Date.now(),
          });
          value.getAudioTracks().forEach(record);
        }
        return originalSrcObject.set.call(this, value);
      },
    });
  }

  window.__probeSnapshot = () => ({
    streams: state.streams,
    tracks: state.tracks.map(({ recorder, ...rest }) => rest),
    errors: state.errors,
    mediaRecorderSupported: state.mediaRecorderSupported,
    videos: Array.from(document.querySelectorAll('video')).map((node) => {
      const rect = node.getBoundingClientRect();
      const stream = node.srcObject;
      return {
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        videoWidth: node.videoWidth,
        videoHeight: node.videoHeight,
        paused: node.paused,
        audioTracks: stream && stream.getAudioTracks ? stream.getAudioTracks().length : 0,
        videoTracks: stream && stream.getVideoTracks ? stream.getVideoTracks().length : 0,
      };
    }),
  });

  window.__probeStop = () => {
    Object.values(state.__recorders || {}).forEach((recorder) => {
      try { recorder.stop(); } catch (e) {}
    });
  };
})();
"""

NAME_INPUTS = [
    'input[name="displayName"]',
    'input[placeholder*="имя" i]',
    'input[placeholder*="name" i]',
    'input[name="name"]',
    'input[type="text"]',
]

PASSWORD_INPUTS = [
    'input[type="password"]',
    'input[placeholder*="ароль" i]',
    'input[placeholder*="password" i]',
    'input[name="password"]',
]

JOIN_BUTTONS = [
    'button:has-text("Подключиться")',
    'button:has-text("Присоединиться")',
    'button:has-text("Войти в комнату")',
    'button:has-text("Войти")',
    'button:has-text("Join")',
    'button:has-text("Enter")',
]

PERMISSION_BUTTONS = [
    'button:has-text("Разрешить")',
    'button:has-text("Позволить")',
    'button:has-text("Allow")',
]

#: The web client runs its own browser check and, for anything it does not
#: recognise, shows "Ваш браузер не поддерживается" with only an "open the app"
#: button. Two things make it pass: no ``--enable-automation`` switch (Playwright
#: adds it by default) and its own sessionStorage bypass, which is what its
#: "continue anyway" button writes.
PRELUDE_JS = """
try { delete navigator.webdriver; } catch (e) {}
try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
try { sessionStorage.setItem('s2b.browserChecker.skipChecking', 'true'); } catch (e) {}
"""

LAUNCH_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-blink-features=AutomationControlled",
]

#: SaluteJazz sends invites with an encrypted ``?psw=`` parameter. That form is a
#: deep link for the desktop app: the web client logs "browser not supported" and
#: fires ``jazz://join``. The same room without ``?psw=`` opens the web lobby with
#: a name and password field, which is what an unattended agent needs.
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


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def try_join(
    page,
    *,
    name: str = "Ассистент (транскрибация)",
    password: str | None = None,
    timeout_ms: int = 30_000,
) -> list[str]:
    """Fill the lobby and press join, retrying until the SPA has rendered.

    The client is a React SPA that fetches feature flags before painting the
    lobby, so the controls appear a few seconds late — a single look is not
    enough, which is exactly how the first attempt failed.
    """
    actions: list[str] = []
    deadline = time.time() + timeout_ms / 1000
    filled_name = filled_password = clicked = False

    while time.time() < deadline and not clicked:
        for selector in PERMISSION_BUTTONS:
            locator = page.locator(selector).first
            try:
                if locator.count() and locator.is_visible():
                    locator.click(timeout=2000)
                    actions.append(f"dismissed media dialog via {selector}")
                    break
            except Exception:  # noqa: BLE001 - heuristics only
                continue

        if not filled_name:
            for selector in NAME_INPUTS:
                locator = page.locator(selector).first
                try:
                    if locator.count() and locator.is_visible() and locator.is_editable():
                        locator.fill(name, timeout=2000)
                        actions.append(f"filled name via {selector}")
                        filled_name = True
                        break
                except Exception:  # noqa: BLE001
                    continue

        if password and not filled_password:
            for selector in PASSWORD_INPUTS:
                locator = page.locator(selector).first
                try:
                    if locator.count() and locator.is_visible():
                        locator.fill(password, timeout=2000)
                        actions.append(f"filled password via {selector}")
                        filled_password = True
                        break
                except Exception:  # noqa: BLE001
                    continue

        if filled_name and (filled_password or not password):
            for selector in JOIN_BUTTONS:
                locator = page.locator(selector).first
                try:
                    if locator.count() and locator.is_visible() and locator.is_enabled():
                        locator.click(timeout=3000)
                        actions.append(f"clicked {selector}")
                        clicked = True
                        break
                except Exception:  # noqa: BLE001
                    continue

        if not clicked:
            page.wait_for_timeout(700)

    if not clicked:
        actions.append("join button never became available")
    return actions


def describe_page(page) -> dict:
    """What is actually on screen — makes a failed join self-explanatory."""
    try:
        return page.evaluate(
            """
            (() => ({
              url: location.href,
              inputs: Array.from(document.querySelectorAll('input')).map((input) => ({
                type: input.type, placeholder: input.placeholder || '', visible: !!input.offsetParent,
              })),
              buttons: Array.from(document.querySelectorAll('button'))
                .filter((button) => button.offsetParent)
                .map((button) => (button.innerText || '').trim().slice(0, 40)),
              bodyStart: (document.body.innerText || '').slice(0, 400),
            }))()
            """
        )
    except Exception as error:  # noqa: BLE001
        return {"error": str(error)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe a Jazz meeting for capturable media tracks")
    parser.add_argument("--url", required=True, help="meeting URL")
    parser.add_argument("--seconds", type=int, default=45, help="observation window")
    parser.add_argument("--headed", action="store_true", help="show the browser (recommended first run)")
    parser.add_argument("--out", default=None, help="output directory")
    parser.add_argument("--password", default=None, help="meeting password (SaluteJazz: 'Пароль видеовстречи')")
    parser.add_argument("--name", default="Ассистент (транскрибация)", help="display name the agent joins with")
    parser.add_argument(
        "--channel",
        default="chrome",
        help="chrome | msedge | chromium — SaluteJazz's gate is browser dependent",
    )
    parser.add_argument(
        "--no-fake-media",
        action="store_true",
        help="do not install synthetic camera/mic (closest to a real user)",
    )
    args = parser.parse_args()

    meeting_url, dropped = strip_deep_link_params(args.url)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else Path(__file__).resolve().parent / "out" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict = {"url": args.url, "started": stamp, "chunks": [], "actions": []}
    if dropped:
        # Invite links carry ?psw=, which makes the web client hand off to the
        # desktop app instead of showing the lobby. The same room works without it.
        report["dropped_params"] = dropped
        report["url_used"] = meeting_url
        log(f"dropped {dropped} from the URL: the web lobby lives at {meeting_url}")

    with sync_playwright() as playwright:
        browser = None
        launch_args = [arg for arg in LAUNCH_ARGS if not (args.no_fake_media and "fake" in arg)]
        channels = [args.channel] + ([] if args.channel == "chromium" else ["chromium"])
        for channel in channels:
            try:
                browser = playwright.chromium.launch(
                    channel=None if channel == "chromium" else channel,
                    headless=not args.headed,
                    args=launch_args,
                    ignore_default_args=["--enable-automation"],
                )
                report["browser"] = channel
                break
            except Exception as error:  # noqa: BLE001
                report.setdefault("launch_errors", []).append(str(error))
        if browser is None:
            print(json.dumps(report.get("launch_errors"), indent=2))
            return 2

        context = browser.new_context(
            permissions=["microphone", "camera"],
            viewport={"width": 1440, "height": 900},
            locale="ru-RU",
        )
        page = context.new_page()
        page.on("pageerror", lambda error: report.setdefault("page_errors", []).append(str(error)))

        page.expose_binding(
            "__probeWrite",
            lambda source, payload: _save_chunk(out_dir, source, payload, report),
        )
        page.add_init_script(PRELUDE_JS)
        page.add_init_script(HOOK)

        log(f"opening {meeting_url}")
        page.goto(meeting_url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(4000)

        page.screenshot(path=str(out_dir / "01-landing.png"))
        report["title"] = page.title()
        report["actions"] = try_join(page, name=args.name, password=args.password)
        log(f"join: {report['actions']}")
        report["after_join"] = describe_page(page)
        page.screenshot(path=str(out_dir / "02-after-join.png"))

        deadline = time.time() + args.seconds
        shot = 0
        while time.time() < deadline:
            page.wait_for_timeout(5000)
            shot += 1
            page.screenshot(path=str(out_dir / f"02-meeting-{shot}.png"))
            snapshot = page.evaluate("window.__probeSnapshot ? window.__probeSnapshot() : null")
            audio_tracks = sum(
                1 for track in (snapshot or {}).get("tracks", []) if track.get("kind") == "audio"
            )
            log(f"tracks: {len((snapshot or {}).get('tracks', []))} total, {audio_tracks} audio")

        report["snapshot"] = page.evaluate("window.__probeSnapshot ? window.__probeSnapshot() : null")
        report["dom"] = page.evaluate("""
            (() => {
              const texts = [];
              document.querySelectorAll('[class*="participant"], [class*="tile"], [class*="user"]').forEach((node) => {
                const label = (node.getAttribute('aria-label') || node.textContent || '').trim().slice(0, 60);
                if (label) texts.push(label);
              });
              return {
                url: location.href,
                bodyText: document.body.innerText.slice(0, 1500),
                candidates: Array.from(new Set(texts)).slice(0, 40),
                videos: document.querySelectorAll('video').length,
              };
            })()
        """)

        try:
            page.evaluate("window.__probeStop && window.__probeStop()")
        except Exception as error:  # noqa: BLE001
            report.setdefault("page_errors", []).append(f"stop: {error}")
        page.wait_for_timeout(2500)

        browser.close()

    report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    report["verdict"] = _verdict(report)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 72)
    print(f"report: {out_dir / 'report.json'}")
    print(f"verdict: {report['verdict']}")
    print("=" * 72)
    return 0 if report["verdict"]["per_participant_audio"] else 1


def _save_chunk(out_dir: Path, source: str, payload: str, report: dict) -> None:
    import base64

    data = base64.b64decode(payload)
    path = out_dir / f"audio-{source[:8]}-{len(report['chunks'])}.webm"
    path.write_bytes(data)
    report["chunks"].append({"file": path.name, "bytes": len(data), "track": source})
    log(f"saved {path.name} ({len(data)} bytes)")


def _verdict(report: dict) -> dict:
    snapshot = report.get("snapshot") or {}
    tracks = snapshot.get("tracks") or []
    audio = [track for track in tracks if track.get("kind") == "audio"]
    streams_with_audio = sum(1 for stream in snapshot.get("streams", []) if any(t.get("kind") == "audio" for t in stream.get("tracks", [])))
    videos = snapshot.get("videos") or []

    return {
        "joined": bool(videos) or bool(tracks),
        "per_participant_audio": len({track.get("id") for track in audio}) > 1,
        "audio_track_count": len({track.get("id") for track in audio}),
        "streams_with_audio": streams_with_audio,
        "video_tiles": len(videos),
        "largest_tile": max(({"w": v.get("videoWidth"), "h": v.get("videoHeight")} for v in videos), key=lambda v: (v["w"] or 0) * (v["h"] or 0), default=None),
        "media_recorder": snapshot.get("mediaRecorderSupported", False),
        "chunks_saved": len(report.get("chunks", [])),
        "next_step": _next_step(len({track.get("id") for track in audio}), len(report.get("chunks", []))),
    }


def _next_step(audio_tracks: int, chunks: int) -> str:
    if audio_tracks > 1:
        return "per-track pipeline (path C): ASR per participant, speaker from the track"
    if audio_tracks == 1:
        return "mixed audio: keep pyannote/embedding diarization as the speaker source"
    if chunks:
        return "tracks appeared late; increase --seconds and check the screenshots"
    return "no media captured: check the join flow, lobby and SSO requirements first"


if __name__ == "__main__":
    sys.exit(main())
