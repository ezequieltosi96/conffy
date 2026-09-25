"""
Valkey access layer. Every read and write to Valkey goes through here, using
the keys and encodings from contracts.py. Works with Valkey and Redis alike.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from redis.asyncio import Redis

from conffy.contracts import (
    CAPTIONS_STREAM_MAXLEN,
    Caption,
    CaptionKind,
    Keys,
    LangCode,
    SessionConfig,
    SessionState,
    StreamMessage,
    decode_message,
    encode_message,
)

StreamEntry = tuple[str, StreamMessage]  # (stream id, message)

# Lease scripts: only the owner may renew or release.
_RENEW = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"
# Worker state writes must never undo a stop requested through the API.
_SET_IF_NOT_ENDED = (
    "local cur = redis.call('get', KEYS[1]) "
    "if cur and cjson.decode(cur)['status'] == 'ended' then return 0 end "
    "redis.call('set', KEYS[1], ARGV[1]) return 1"
)
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


def connect(url: str) -> Redis:
    # bytes in, bytes out: audio streams carry raw PCM
    return Redis.from_url(url, decode_responses=False, health_check_interval=30)


def _s(v: bytes | str) -> str:
    return v.decode() if isinstance(v, bytes) else v


# ----------------------------------------------------------------- sessions
async def save_session(r: Redis, cfg: SessionConfig, *, overwrite: bool = False) -> bool:
    """Returns False if it already existed and overwrite is False."""
    created = await r.set(Keys.config(cfg.id), cfg.model_dump_json(), nx=not overwrite)
    if not created:
        return False
    await r.sadd(Keys.SESSIONS, cfg.id)
    if not await r.exists(Keys.state(cfg.id)):
        await set_state(r, cfg.id, SessionState())
    return True


async def get_session(r: Redis, sid: str) -> SessionConfig | None:
    raw = await r.get(Keys.config(sid))
    return SessionConfig.model_validate_json(raw) if raw else None


async def session_ids(r: Redis) -> list[str]:
    return sorted(_s(x) for x in await r.smembers(Keys.SESSIONS))


async def get_state(r: Redis, sid: str) -> SessionState:
    raw = await r.get(Keys.state(sid))
    return SessionState.model_validate_json(raw) if raw else SessionState()


async def set_state(r: Redis, sid: str, state: SessionState) -> None:
    await r.set(Keys.state(sid), state.model_dump_json())


async def set_state_unless_ended(r: Redis, sid: str, state: SessionState) -> bool:
    """Atomic: writes only if nobody marked the session ENDED meanwhile."""
    return bool(await r.eval(_SET_IF_NOT_ENDED, 1, Keys.state(sid), state.model_dump_json()))


def load_sessions_file(path: str | Path) -> list[SessionConfig]:
    p = Path(path)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [SessionConfig.model_validate(s) for s in data.get("sessions") or []]


# ----------------------------------------------------------------- leases
async def acquire_lease(r: Redis, sid: str, owner: str, ttl_ms: int) -> bool:
    return bool(await r.set(Keys.lease(sid), owner, nx=True, px=ttl_ms))


async def renew_lease(r: Redis, sid: str, owner: str, ttl_ms: int) -> bool:
    return bool(await r.eval(_RENEW, 1, Keys.lease(sid), owner, ttl_ms))


async def release_lease(r: Redis, sid: str, owner: str) -> None:
    await r.eval(_RELEASE, 1, Keys.lease(sid), owner)


# ----------------------------------------------------------------- captions
async def publish(r: Redis, msg: StreamMessage, lang: LangCode) -> str:
    sid = msg.session_id
    entry_id = await r.xadd(
        Keys.captions(sid, lang), encode_message(msg), maxlen=CAPTIONS_STREAM_MAXLEN, approximate=True
    )
    return _s(entry_id)


def _decode(entries: Iterable) -> list[StreamEntry]:
    return [(_s(eid), decode_message(fields)) for eid, fields in entries]


async def read_range(r: Redis, sid: str, lang: LangCode, after: str = "-", count: int | None = None) -> list[StreamEntry]:
    """Entries strictly after `after` (or from the start with "-")."""
    start = "-" if after == "-" else f"({after}"
    return _decode(await r.xrange(Keys.captions(sid, lang), min=start, max="+", count=count))


async def last_finals(r: Redis, sid: str, lang: LangCode, n: int, scan: int = 500) -> list[StreamEntry]:
    """The last n FINAL captions, oldest first."""
    rows = _decode(await r.xrevrange(Keys.captions(sid, lang), count=scan))
    finals = [(i, m) for i, m in rows if isinstance(m, Caption) and m.kind is CaptionKind.FINAL]
    return list(reversed(finals[:n]))


async def all_finals(r: Redis, sid: str, lang: LangCode) -> list[Caption]:
    """Every FINAL, one per seq (the latest wins if a restart repeated a seq), in time order."""
    by_seq: dict[int, Caption] = {}
    for _, m in await read_range(r, sid, lang):
        if isinstance(m, Caption) and m.kind is CaptionKind.FINAL:
            by_seq[m.seq] = m
    return sorted(by_seq.values(), key=lambda c: (c.t0, c.seq))


async def stream_tail(r: Redis, sid: str, lang: LangCode) -> str:
    """Id of the newest entry, or '0-0' for an empty stream."""
    rows = await r.xrevrange(Keys.captions(sid, lang), count=1)
    return _s(rows[0][0]) if rows else "0-0"
