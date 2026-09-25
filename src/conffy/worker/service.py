"""
Worker service. Each process claims one session at a time from Valkey (lease),
runs its pipeline and publishes captions. Scale with:

    docker compose up --scale worker=N

Lifecycle of a session seen from the worker:
    WAITING --claim--> LIVE --source ends--> ENDED
                        |---stop via API---> ENDED
                        |---source fails---> ERROR
                        '---worker dies----> (lease expires) another worker resumes it
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from pysilero_vad import SileroVoiceActivityDetector
from redis.asyncio import Redis

from conffy import bus
from conffy.contracts import (
    LEASE_RENEW_EVERY_S,
    LEASE_TTL_MS,
    Caption,
    SessionConfig,
    SessionEvent,
    SessionEventKind,
    SessionState,
    SessionStatus,
    SourceKind,
)
from conffy.glossary import EMPTY, Glossary, load_glossary
from conffy.providers.registry import make_transcriber, make_translator
from conffy.settings import Settings
from conffy.worker.audio import ffmpeg_pcm
from conffy.worker.pipeline import AsrPipeline, PipelineConfig
from conffy.worker.segmenter import Segmenter
from conffy.worker.translate import TranslationStage

log = logging.getLogger("conffy.worker")

IDLE_POLL_S = 2.0
DRAIN_TIMEOUT_S = 15.0


async def claim_next(r: Redis, owner: str) -> SessionConfig | None:
    for sid in await bus.session_ids(r):
        state = await bus.get_state(r, sid)
        if state.status in (SessionStatus.ENDED, SessionStatus.ERROR):
            continue
        cfg = await bus.get_session(r, sid)
        if cfg is None or cfg.source.kind is SourceKind.BROWSER:
            continue  # browser ingest is not implemented yet
        if await bus.acquire_lease(r, sid, owner, LEASE_TTL_MS):
            return cfg
    return None


def _glossary(settings: Settings, cfg: SessionConfig) -> Glossary:
    if not cfg.glossary:
        return EMPTY
    try:
        return load_glossary(settings.glossary_file, cfg.glossary)
    except (OSError, KeyError) as e:
        log.warning("session %s: glossary unavailable (%s), continuing without it", cfg.id, e)
        return EMPTY


async def run_session(r: Redis, cfg: SessionConfig, settings: Settings, owner: str) -> None:
    sid = cfg.id
    glossary = _glossary(settings, cfg)

    # Resume numbering and clock after a failover or a restart
    last = await bus.last_finals(r, sid, cfg.source_lang, 1)
    start_seq = last[0][1].seq + 1 if last else 0
    offset = last[0][1].t1 if last else 0.0

    state = SessionState(status=SessionStatus.LIVE, worker_id=owner, started_at=time.time())
    await bus.set_state_unless_ended(r, sid, state)
    log.info("session %s: live (seq from %d, offset %.1fs)", sid, start_seq, offset)

    transcriber = make_transcriber(settings)
    translator = make_translator(settings)

    async def publish_translation(c: Caption) -> None:
        await bus.publish(r, c, c.lang)

    stage = TranslationStage(
        translator,
        publish_translation,
        source_lang=cfg.source_lang,
        target_langs=cfg.target_langs,
        glossaries={lang: glossary.for_lang(lang) for lang in cfg.target_langs},
    )

    async def publish_source(c: Caption) -> None:
        await bus.publish(r, c, c.lang)
        stage.submit(c)
        state.last_caption_at = time.time()

    pipeline = AsrPipeline(
        transcriber,
        Segmenter(SileroVoiceActivityDetector()),
        publish_source,
        PipelineConfig(
            session_id=sid,
            language=cfg.source_lang,
            base_prompt=glossary.asr_prompt(),
            start_seq=start_seq,
            time_offset_s=offset,
        ),
    )

    async def keeper() -> str:
        """Renews the lease, reports metrics, and notices stops. Returns why it quit."""
        while True:
            await asyncio.sleep(LEASE_RENEW_EVERY_S)
            if not await bus.renew_lease(r, sid, owner, LEASE_TTL_MS):
                return "lost"
            state.updated_at = time.time()
            state.asr_ms = round(pipeline.stats.last_asr_ms, 1)
            state.mt_ms = round(stage.stats.last_mt_ms, 1)
            state.audio_lag_s = round(pipeline.stats.last_lag_s, 2)
            if not await bus.set_state_unless_ended(r, sid, state):
                return "stopped"

    mt_task = asyncio.create_task(stage.run(), name=f"mt:{sid}")
    pipe_task = asyncio.create_task(pipeline.run(ffmpeg_pcm(cfg.source)), name=f"asr:{sid}")
    keep_task = asyncio.create_task(keeper(), name=f"lease:{sid}")
    outcome, error = "ended", None
    try:
        done, _ = await asyncio.wait({pipe_task, keep_task}, return_when=asyncio.FIRST_COMPLETED)
        failed = next((t for t in (keep_task, pipe_task) if t in done and t.exception()), None)
        if failed is not None:
            exc = failed.exception()
            outcome, error = "error", f"{type(exc).__name__}: {exc}"
        elif keep_task in done:
            outcome = keep_task.result()
    except asyncio.CancelledError:
        outcome = "shutdown"
        raise
    finally:
        for t in (pipe_task, keep_task):
            t.cancel()
        await asyncio.gather(pipe_task, keep_task, return_exceptions=True)
        stage.close()
        if outcome == "lost":
            mt_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(mt_task), DRAIN_TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError):
            mt_task.cancel()
        await asyncio.gather(mt_task, return_exceptions=True)
        await transcriber.aclose()
        await translator.aclose()
        await _finish(r, cfg, owner, state, outcome, error)


async def _finish(
    r: Redis, cfg: SessionConfig, owner: str, state: SessionState, outcome: str, error: str | None
) -> None:
    sid = cfg.id
    log.info("session %s: %s%s", sid, outcome, f" ({error})" if error else "")
    if outcome == "lost":
        return  # another worker owns it now: touch nothing
    if outcome == "shutdown":
        # hand it back so another worker picks it up right away
        state.status, state.worker_id = SessionStatus.WAITING, None
        await bus.set_state_unless_ended(r, sid, state)
    else:
        kind = SessionEventKind.ERROR if outcome == "error" else SessionEventKind.ENDED
        if outcome != "stopped":
            state.status = SessionStatus.ERROR if outcome == "error" else SessionStatus.ENDED
            state.error = error
            state.updated_at = time.time()
            await bus.set_state(r, sid, state)
        for lang in cfg.langs:
            await bus.publish(r, SessionEvent(session_id=sid, kind=kind, detail=error), lang)
    await bus.release_lease(r, sid, owner)


async def serve(settings: Settings, stop: asyncio.Event) -> None:
    r = bus.connect(settings.valkey_url)
    owner = settings.worker_id
    while not stop.is_set():  # wait for Valkey
        try:
            await r.ping()
            break
        except Exception as e:
            log.warning("waiting for Valkey at %s: %s", settings.valkey_url, e)
            await asyncio.sleep(2)
    log.info("worker %s ready", owner)
    try:
        while not stop.is_set():
            cfg = await claim_next(r, owner)
            if cfg is None:
                try:
                    await asyncio.wait_for(stop.wait(), IDLE_POLL_S)
                except TimeoutError:
                    pass
                continue
            session = asyncio.create_task(run_session(r, cfg, settings, owner))
            stopper = asyncio.create_task(stop.wait())
            await asyncio.wait({session, stopper}, return_when=asyncio.FIRST_COMPLETED)
            stopper.cancel()
            if not session.done():
                session.cancel()
            results = await asyncio.gather(session, return_exceptions=True)
            if isinstance(results[0], Exception) and not isinstance(results[0], asyncio.CancelledError):
                log.exception("session %s crashed", cfg.id, exc_info=results[0])
    finally:
        await r.aclose()
        log.info("worker %s stopped", owner)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per model call is noise
    settings = Settings()

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await serve(settings, stop)

    asyncio.run(run())


if __name__ == "__main__":
    main()
