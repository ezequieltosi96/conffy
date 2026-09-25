"""
conffy API. Stateless: everything lives in Valkey, so it runs as N replicas
behind nginx.

    uv run uvicorn conffy.api.main:app --reload     # dev, serves web/ too
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from conffy import bus
from conffy.api.fanout import Fanout
from conffy.bus import StreamEntry
from conffy.contracts import (
    SSE_EVENT_CAPTION,
    SSE_EVENT_SESSION,
    Caption,
    SessionConfig,
    SessionState,
    SessionStatus,
)
from conffy.export import FORMATS
from conffy.settings import Settings

log = logging.getLogger("conffy.api")
_STREAM_ID = re.compile(r"^\d+-\d+$")


class SessionView(BaseModel):
    config: SessionConfig
    state: SessionState


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    r = bus.connect(settings.valkey_url)
    created = 0
    for cfg in bus.load_sessions_file(settings.sessions_file):
        created += await bus.save_session(r, cfg)
    log.info("seeded %d new session(s) from %s", created, settings.sessions_file)
    app.state.r = r
    app.state.fanout = Fanout(r)
    yield
    await app.state.fanout.close()
    await r.aclose()


app = FastAPI(title="conffy", version="0.1.0", lifespan=lifespan)


async def _session(request: Request, sid: str) -> SessionConfig:
    cfg = await bus.get_session(request.app.state.r, sid)
    if cfg is None:
        raise HTTPException(404, f"session {sid!r} not found")
    return cfg


# ----------------------------------------------------------------- health
@app.get("/api/health")
async def health(request: Request) -> dict:
    await request.app.state.r.ping()
    return {"status": "ok", "viewers": request.app.state.fanout.viewers, "time": time.time()}


# ----------------------------------------------------------------- sessions
@app.get("/api/sessions")
async def list_sessions(request: Request) -> list[SessionView]:
    r = request.app.state.r
    views = []
    for sid in await bus.session_ids(r):
        if cfg := await bus.get_session(r, sid):
            views.append(SessionView(config=cfg, state=await bus.get_state(r, sid)))
    return views


@app.post("/api/sessions", status_code=201)
async def create_session(request: Request, cfg: SessionConfig) -> SessionView:
    r = request.app.state.r
    if not await bus.save_session(r, cfg):
        raise HTTPException(409, f"session {cfg.id!r} already exists")
    return SessionView(config=cfg, state=await bus.get_state(r, cfg.id))


@app.get("/api/sessions/{sid}")
async def get_session(request: Request, sid: str) -> SessionView:
    cfg = await _session(request, sid)
    return SessionView(config=cfg, state=await bus.get_state(request.app.state.r, sid))


@app.post("/api/sessions/{sid}/stop")
async def stop_session(request: Request, sid: str) -> SessionState:
    await _session(request, sid)
    r = request.app.state.r
    state = await bus.get_state(r, sid)
    state.status, state.updated_at = SessionStatus.ENDED, time.time()
    await bus.set_state(r, sid, state)  # the worker notices within a few seconds
    return state


@app.post("/api/sessions/{sid}/start")
async def start_session(request: Request, sid: str) -> SessionState:
    """(Re)start an ended or failed session: any free worker picks it up."""
    await _session(request, sid)
    r = request.app.state.r
    state = await bus.get_state(r, sid)
    if state.status is SessionStatus.LIVE:
        return state
    state = SessionState(status=SessionStatus.WAITING)
    await bus.set_state(r, sid, state)
    return state


# ----------------------------------------------------------------- captions
def _sse(entry: StreamEntry | None) -> str:
    if entry is None:
        return ": keep-alive\n\n"
    entry_id, msg = entry
    event = SSE_EVENT_CAPTION if isinstance(msg, Caption) else SSE_EVENT_SESSION
    return f"id: {entry_id}\nevent: {event}\ndata: {msg.model_dump_json()}\n\n"


@app.get("/api/sessions/{sid}/captions/{lang}/stream")
async def caption_stream(
    request: Request,
    sid: str,
    lang: str,
    last_event_id: str | None = Header(default=None),
    from_: str | None = Query(default=None, alias="from"),
) -> StreamingResponse:
    cfg = await _session(request, sid)
    if lang not in cfg.langs:
        raise HTTPException(404, f"session {sid!r} has no {lang!r} captions (available: {', '.join(cfg.langs)})")
    resume = last_event_id or from_
    if resume and not _STREAM_ID.match(resume):
        resume = None

    async def body() -> AsyncIterator[str]:
        yield "retry: 2000\n\n"  # browsers reconnect after 2 s, sending Last-Event-ID
        async with aclosing(request.app.state.fanout.events(sid, lang, resume)) as events:
            async for entry in events:
                yield _sse(entry)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/sessions/{sid}/export.{fmt}")
async def export(request: Request, sid: str, fmt: str, lang: str | None = None) -> Response:
    cfg = await _session(request, sid)
    if fmt not in FORMATS:
        raise HTTPException(404, f"unknown format {fmt!r} (available: {', '.join(FORMATS)})")
    lang = lang or cfg.source_lang
    if lang not in cfg.langs:
        raise HTTPException(404, f"session {sid!r} has no {lang!r} captions")
    render, media_type = FORMATS[fmt]
    captions = await bus.all_finals(request.app.state.r, sid, lang)
    return Response(
        render(captions),
        media_type=f"{media_type}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{sid}-{lang}.{fmt}"'},
    )


# ----------------------------------------------------------------- dev convenience
# In docker compose nginx serves web/; running the API alone serves it too.
_WEB = Path(__file__).resolve().parents[3] / "web"
if _WEB.is_dir():
    app.mount("/", StaticFiles(directory=_WEB, html=True), name="web")
