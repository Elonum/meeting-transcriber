"""HTTP + WebSocket API.

Routes
------
GET    /api/health                       liveness + which pipelines are available
GET    /api/sessions                     list sessions, newest first
POST   /api/sessions                     start an agent for a meeting URL
GET    /api/sessions/{id}                snapshot (participants + final segments)
POST   /api/sessions/{id}/stop           stop the agent
GET    /api/sessions/{id}/export/{fmt}   download transcript (srt|vtt|txt|md|json)
GET    /api/sessions/{id}/log            raw event log (JSONL) — audit trail
WS     /events/{id}                      live event stream for one session

The socket is server -> client only. State-changing calls are plain REST so that
every mutation has an idempotent, auditable entry point.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.background import BackgroundTask

from . import auth, retention
from . import export as exporters
from . import protocol as p
from .bundle import BUNDLE_NAME, build_bundle
from .mock import MockPipeline
from .recording import read_recordings
from .session import SessionManager

log = logging.getLogger("transcribe")

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("TRANSCRIBE_DATA_DIR", ROOT / "data"))
STATIC_DIR = Path(os.environ.get("TRANSCRIBE_STATIC_DIR", ROOT / "frontend" / "dist"))
LOG_FILE = DATA_DIR / "app.log"


def configure_logging() -> None:
    """Console for the terminal, a rotating file for when the terminal is gone.

    The UI cannot show a Python traceback, and asking a user to reproduce a crash
    so a log can be read is a bad trade. Everything the service logs lands in
    ``data/app.log`` as well, and ``GET /api/logs`` returns the tail.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
            )
        )
    except OSError:  # noqa: PERF203 - a read-only data dir must not stop the API
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


configure_logging()

manager = SessionManager(DATA_DIR)


@asynccontextmanager
async def lifespan(_: FastAPI):
    loaded = manager.load_from_disk()
    if loaded:
        log.info("restored %d session(s) from %s", loaded, DATA_DIR)
    for session in manager.due_scheduled():
        session.pipeline = _build_pipeline(
            session.meeting_url,
            pipeline=session.pipeline.name,
            headless=bool(session.options.get("headless", True)),
            model=str(session.options.get("model", "large-v3-turbo")),
            speed=float(session.options.get("speed", 1.0)),
            password=session.options.get("password"),
            language=session.options.get("language"),
        )
        await session.start(speed=float(session.options.get("speed", 1.0)))
        log.info("re-armed scheduled session %s", session.id)
    if retention.retention_days():
        log.info("retention: keeping meetings for %d day(s)", retention.retention_days())
    sweeper = asyncio.create_task(retention.run_forever(manager))
    try:
        yield
    finally:
        sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeper


app = FastAPI(
    title="Transcribe — онлайн-транскрибация встреч",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(auth.TokenMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartRequest(BaseModel):
    meeting_url: str = Field(..., description="Ссылка на встречу Jazz")
    title: str | None = Field(None, max_length=200)
    duration_min: int | None = Field(None, ge=1, le=600)
    start_at: datetime | None = Field(
        None, description="Когда подключаться (ISO 8601). Пусто — сразу"
    )
    pipeline: Literal["mock", "jazz"] = Field(
        "mock", description="mock — сценарная демонстрация; jazz — настоящий браузерный агент"
    )
    speed: float = Field(1.0, ge=0.05, le=50.0, description="Ускорение mock-пайплайна")
    headless: bool = Field(True, description="Для jazz: показывать ли браузер")
    model: str = Field("large-v3-turbo", description="Для jazz: модель faster-whisper")
    password: str | None = Field(
        None, description="Пароль встречи, если ссылка без встроенного ?psw="
    )
    language: Literal["auto", "ru", "en"] = Field(
        "auto",
        description="Язык речи: auto — определять по фразе, ru/en — зафиксировать",
    )

    @field_validator("meeting_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("нужна полная ссылка вида https://…")
        return value.strip()


class SessionResponse(BaseModel):
    session: dict
    participants: list[dict]
    segments: list[dict]


def _require(session_id: str):
    _validate_id(session_id)
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="сессия не найдена")
    return session


#: Session ids are generated by us; anything else is a probing attempt.
SESSION_ID = re.compile(r"^[0-9a-f]{6,32}$")
#: Only files this service writes may be served back.
AUDIO_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}\.wav$")


def _validate_id(session_id: str) -> None:
    if not SESSION_ID.match(session_id):
        raise HTTPException(status_code=404, detail="сессия не найдена")


async def export_bundle(session_id: str) -> FileResponse:
    """Everything in one archive; the temp file is removed after the response."""
    session = _require(session_id)
    try:
        path = build_bundle(session)
    except Exception as error:  # noqa: BLE001 - surfaced to the client
        log.exception("bundle: failed for %s", session_id)
        raise HTTPException(status_code=500, detail=f"не удалось собрать архив: {error}") from error
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"{_slug(session.title)}.zip" if session.title else BUNDLE_NAME,
        background=BackgroundTask(_cleanup, path),
    )


def _cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # noqa: BLE001 - a leftover file is not worth an error
        log.warning("bundle: could not remove %s", path)


@app.get("/api/health")
async def health() -> dict:
    try:
        import faster_whisper  # noqa: F401

        asr_ready = True
    except Exception:  # noqa: BLE001
        asr_ready = False
    return {
        "status": "ok",
        "pipelines": {"mock": True, "jazz": asr_ready},
        "asr": {"faster_whisper": asr_ready},
        "sessions": len(manager.sessions),
        "data_dir": str(DATA_DIR),
        "static_ready": (STATIC_DIR / "index.html").is_file(),
        "log_file": str(LOG_FILE),
        "auth_required": auth.auth_enabled(),
        "retention_days": retention.retention_days(),
    }


@app.get("/api/logs")
async def logs(lines: int = 200) -> Response:
    """Tail of the service log, so "где смотреть логи" has an answer in the UI."""
    limit = max(1, min(lines, 5000))
    if not LOG_FILE.is_file():
        return Response(content="", media_type="text/plain; charset=utf-8")
    with LOG_FILE.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 200_000))
        tail = handle.read().decode("utf-8", "replace")
    return Response(content="\n".join(tail.splitlines()[-limit:]), media_type="text/plain; charset=utf-8")


@app.get("/api/auth")
async def auth_status(request: Request) -> dict:
    """Tells the interface whether a token is needed and whether it has one."""
    return auth.status(request)


@app.post("/api/auth")
async def auth_login(payload: auth.AuthRequest, response: Response) -> dict:
    """Exchange the token for an HttpOnly cookie; a wrong token is a plain 401."""
    return auth.login(payload, response)


@app.get("/api/sessions")
async def list_sessions(q: str | None = None, limit: int = 50) -> dict:
    """History, newest first. ``q`` searches titles, links, names and speech.

    ``total`` is the number of matches before the limit, so the interface can say
    "показаны последние 50 из 90" instead of silently hiding the rest.
    """
    query = (q or "").strip()
    results = manager.search(query)
    page = max(1, min(limit, 200))
    sessions = []
    for session in results[:page]:
        item = session.as_dict()
        if query:
            item["snippet"] = session.snippet(query)
        sessions.append(item)
    return {"sessions": sessions, "query": query, "total": len(results), "limit": page}


def _build_pipeline(
    meeting_url: str,
    *,
    pipeline: str,
    headless: bool,
    model: str,
    speed: float,
    password: str | None = None,
    language: str | None = None,
):
    if pipeline == "jazz":
        from .jazz import JazzPipeline

        return JazzPipeline(
            meeting_url,
            headless=headless,
            model=model,
            password=password,
            language=None if language in (None, "auto") else language,
        )
    return MockPipeline(meeting_url, speed=speed)


@app.post("/api/sessions", status_code=201)
async def create_session(payload: StartRequest) -> dict:
    start_at_ms: int | None = None
    if payload.start_at is not None:
        moment = payload.start_at
        if moment.tzinfo is None:
            # Naive datetime comes from <input type="datetime-local">; the
            # operator means their own local time, not UTC.
            moment = moment.astimezone()
        start_at_ms = int(moment.timestamp() * 1000)
        if start_at_ms < p.now_ms() - 60_000:
            raise HTTPException(status_code=422, detail="время старта уже прошло")

    pipeline = _build_pipeline(
        payload.meeting_url,
        pipeline=payload.pipeline,
        headless=payload.headless,
        model=payload.model,
        speed=payload.speed,
        password=payload.password,
        language=payload.language,
    )
    session = manager.create(
        payload.meeting_url,
        title=payload.title,
        duration_min=payload.duration_min,
        pipeline=pipeline,
        start_at_ms=start_at_ms,
        options={
            "pipeline": payload.pipeline,
            "headless": payload.headless,
            "model": payload.model,
            "speed": payload.speed,
            "password": payload.password,
            "language": payload.language,
        },
    )
    # Give the agent somewhere to save a screenshot when the join goes wrong.
    setter = getattr(session.pipeline, "set_artifacts_dir", None)
    if callable(setter):
        setter(session.dir)
    await session.start(speed=payload.speed)

    log.info(
        "session %s started (pipeline=%s, start_at=%s)",
        session.id,
        session.pipeline.name,
        moment.isoformat() if start_at_ms else "now",
    )
    return session.as_dict()


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> SessionResponse:
    session = _require(session_id)
    payload = session.as_dict()
    payload["audio"] = [
        {"participant_id": item.participant_id, "file": item.file, "duration_s": item.duration_s}
        for item in read_recordings(session.dir)
    ]
    return SessionResponse(
        session=payload,
        participants=sorted(session.participants.values(), key=lambda x: x["joined_at_ms"]),
        segments=session.final_segments(),
    )


@app.delete("/api/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str) -> Response:
    """Forget a meeting and delete its transcript, log and audio from disk."""
    _validate_id(session_id)
    if not await manager.delete(session_id):
        raise HTTPException(status_code=404, detail="сессия не найдена")
    log.info("session %s deleted", session_id)
    return Response(status_code=204)


@app.post("/api/sessions/{session_id}/stop")
async def stop_session(session_id: str) -> dict:
    session = _require(session_id)
    await session.stop()
    return session.as_dict()


@app.get("/api/sessions/{session_id}/audio/{filename}")
async def session_audio(session_id: str, filename: str) -> FileResponse:
    """Serve one recorded track. The name is validated, not trusted."""
    session = _require(session_id)
    if not AUDIO_NAME.match(filename):
        raise HTTPException(status_code=404, detail="файл не найден")
    path = session.dir / "audio" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="файл не найден")
    return FileResponse(path, media_type="audio/wav", filename=filename)


@app.get("/api/sessions/{session_id}/log")
async def session_log(session_id: str) -> Response:
    session = _require(session_id)
    path = session.dir / "events.jsonl"
    body = path.read_text(encoding="utf-8") if path.is_file() else ""
    return Response(content=body, media_type="application/x-ndjson; charset=utf-8")


@app.get("/api/sessions/{session_id}/export/{fmt}")
async def export_session(session_id: str, fmt: str) -> Response:
    if fmt == "zip":
        return await export_bundle(session_id)

    session = _require(session_id)
    if fmt not in exporters.FORMATS:
        raise HTTPException(status_code=400, detail=f"формат должен быть одним из {exporters.FORMATS}")
    body = exporters.render(
        fmt,
        session.as_dict(),
        session.final_segments(),
        list(session.participants.values()),
    )
    filename = _disposition(session.title, fmt)
    return Response(
        content=body,
        media_type=exporters.CONTENT_TYPES[fmt],
        headers={"Content-Disposition": filename},
    )


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def _slug(title: str) -> str:
    ascii_title = "".join(_TRANSLIT.get(ch, ch) for ch in title.lower())
    return re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-")[:60] or "transcript"


def _disposition(title: str, fmt: str) -> str:
    """ASCII-safe download name plus an RFC 5987 UTF-8 name.

    HTTP headers are latin-1, so a Cyrillic title has to be transliterated for
    the plain ``filename`` and passed quoted in ``filename*`` for modern clients.
    """
    slug = _slug(title)
    return f"attachment; filename=\"{slug}.{fmt}\"; filename*=UTF-8''{quote(slug)}.{fmt}"


@app.websocket("/events/{session_id}")
async def events(socket: WebSocket, session_id: str) -> None:
    if not auth.is_authorised(socket):  # type: ignore[arg-type]
        await socket.close(code=4401)
        return
    session = manager.get(session_id)
    if session is None:
        await socket.close(code=4404)
        return
    await socket.accept()
    queue = session.subscribe()
    try:
        await socket.send_text(json.dumps(session.snapshot(), ensure_ascii=False))
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20.0)
            except TimeoutError:
                await socket.send_text(json.dumps({"type": "ping", "at_ms": 0}))
                continue
            if event.get("type") == "gone":
                await socket.close(code=4404)
                return
            await socket.send_text(json.dumps(event, ensure_ascii=False))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        session.unsubscribe(queue)


if (ROOT / "tools" / "fake_meeting").is_dir():
    # Test double for a real meeting: a local page where participants talk over
    # an actual RTCPeerConnection mesh, so the capture path can be verified.
    app.mount("/fake-meeting", StaticFiles(directory=ROOT / "tools" / "fake_meeting", html=True), name="fake-meeting")

if (ROOT / ".artifacts" / "speech").is_dir():
    app.mount("/fixtures", StaticFiles(directory=ROOT / ".artifacts" / "speech"), name="fixtures")

if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="web")
else:  # pragma: no cover - developer convenience only

    @app.get("/", response_class=HTMLResponse)
    async def placeholder() -> str:
        return (
            "<h1>Фронтенд ещё не собран</h1>"
            "<p>Выполните <code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code>, "
            "затем перезапустите сервер. API доступен: "
            '<a href="/docs">/docs</a>, <a href="/api/health">/api/health</a>.</p>'
        )
