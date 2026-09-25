"""
Fan-out of caption streams to SSE clients.

Each API replica runs at most one XREAD loop per (session, lang) that has
viewers, and copies every entry to its local subscribers. Valkey load grows
with sessions x languages x replicas, never with viewers.

Resume without gaps or duplicates:
  1. attach to the hub (its reader is already positioned at the stream tail)
  2. read the backlog directly from Valkey
  3. yield the backlog, then live entries newer than the backlog's last id
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from redis.asyncio import Redis

from conffy import bus
from conffy.bus import StreamEntry
from conffy.contracts import SSE_KEEPALIVE_S, Keys, LangCode, decode_message

log = logging.getLogger("conffy.api.fanout")

REPLAY_FINALS = 20  # fresh viewers get the last N finals for context
MAX_BACKLOG = 5_000
SUBSCRIBER_QUEUE = 1_000  # a client this far behind is dropped (it reconnects and resumes)
_DROPPED = object()


def _id(entry_id: str) -> tuple[int, int]:
    ms, _, seq = entry_id.partition("-")
    return int(ms), int(seq or 0)


class _Hub:
    def __init__(self, r: Redis, sid: str, lang: LangCode) -> None:
        self._r, self.sid, self.lang = r, sid, lang
        self.subs: set[asyncio.Queue] = set()
        self.ready = asyncio.Event()
        self.task = asyncio.create_task(self._loop(), name=f"hub:{sid}:{lang}")

    async def _loop(self) -> None:
        key = Keys.captions(self.sid, self.lang)
        last = await bus.stream_tail(self._r, self.sid, self.lang)
        self.ready.set()
        while True:
            try:
                resp = await self._r.xread({key: last}, block=5_000, count=200)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("hub %s/%s read failed: %s", self.sid, self.lang, e)
                await asyncio.sleep(1)
                continue
            for _, entries in resp or []:
                for raw_id, fields in entries:
                    last = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
                    item = (last, decode_message(fields))
                    for q in list(self.subs):
                        try:
                            q.put_nowait(item)
                        except asyncio.QueueFull:  # too slow: make room for the drop signal
                            self.subs.discard(q)
                            q.get_nowait()
                            q.put_nowait(_DROPPED)


class Fanout:
    def __init__(self, r: Redis) -> None:
        self._r = r
        self._hubs: dict[tuple[str, LangCode], _Hub] = {}

    @property
    def viewers(self) -> dict[str, int]:
        return {f"{sid}/{lang}": len(h.subs) for (sid, lang), h in self._hubs.items()}

    async def events(self, sid: str, lang: LangCode, resume_from: str | None) -> AsyncIterator[StreamEntry | None]:
        """Yields stream entries; None means 'send a keep-alive'."""
        key = (sid, lang)
        hub = self._hubs.get(key)
        if hub is None:
            hub = self._hubs[key] = _Hub(self._r, sid, lang)
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
        hub.subs.add(q)
        try:
            await hub.ready.wait()
            if resume_from:
                backlog = await bus.read_range(self._r, sid, lang, after=resume_from, count=MAX_BACKLOG)
            else:
                backlog = await bus.last_finals(self._r, sid, lang, REPLAY_FINALS)
            seen = _id(resume_from) if resume_from else (0, 0)
            for entry in backlog:
                seen = max(seen, _id(entry[0]))
                yield entry
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), SSE_KEEPALIVE_S)
                except TimeoutError:
                    yield None
                    continue
                if item is _DROPPED:
                    return
                if _id(item[0]) > seen:
                    seen = _id(item[0])
                    yield item
        finally:
            hub.subs.discard(q)
            if not hub.subs and self._hubs.get(key) is hub:
                del self._hubs[key]
                hub.task.cancel()

    async def close(self) -> None:
        for hub in self._hubs.values():
            hub.task.cancel()
        await asyncio.gather(*(h.task for h in self._hubs.values()), return_exceptions=True)
        self._hubs.clear()
