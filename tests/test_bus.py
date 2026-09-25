"""Integration tests against a real Valkey/Redis. Skipped if none is reachable.

    TEST_VALKEY_URL=redis://localhost:6379/15 uv run pytest tests/test_bus.py
"""
import asyncio
import os
from contextlib import aclosing

import pytest

from conffy import bus
from conffy.api.fanout import Fanout
from conffy.contracts import (
    Caption, CaptionKind, SessionConfig, SessionEvent, SessionEventKind,
    SessionState, SessionStatus, Source,
)

URL = os.environ.get("TEST_VALKEY_URL", "redis://localhost:6379/15")


@pytest.fixture
async def r():
    client = bus.connect(URL)
    try:
        await client.ping()
    except Exception:
        pytest.skip(f"no Valkey/Redis at {URL}")
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


def cfg(sid="sala-a"):
    return SessionConfig(id=sid, title="t", source=Source(kind="file", uri="/x.wav"))


def cap(seq, kind=CaptionKind.FINAL, lang="en"):
    return Caption(session_id="sala-a", lang=lang, kind=kind, seq=seq, text=f"t{seq}", t0=seq, t1=seq + 1)


async def test_sessions_and_state(r):
    assert await bus.save_session(r, cfg())
    assert not await bus.save_session(r, cfg())  # already exists
    assert await bus.session_ids(r) == ["sala-a"]
    assert (await bus.get_session(r, "sala-a")).id == "sala-a"
    assert (await bus.get_state(r, "sala-a")).status is SessionStatus.WAITING


async def test_state_write_never_undoes_a_stop(r):
    await bus.save_session(r, cfg())
    assert await bus.set_state_unless_ended(r, "sala-a", SessionState(status=SessionStatus.LIVE))
    await bus.set_state(r, "sala-a", SessionState(status=SessionStatus.ENDED))
    assert not await bus.set_state_unless_ended(r, "sala-a", SessionState(status=SessionStatus.LIVE))
    assert (await bus.get_state(r, "sala-a")).status is SessionStatus.ENDED


async def test_lease_is_exclusive_and_owner_only(r):
    assert await bus.acquire_lease(r, "sala-a", "w1", 5000)
    assert not await bus.acquire_lease(r, "sala-a", "w2", 5000)
    assert not await bus.renew_lease(r, "sala-a", "w2", 5000)
    assert await bus.renew_lease(r, "sala-a", "w1", 5000)
    await bus.release_lease(r, "sala-a", "w2")  # not the owner: no effect
    assert not await bus.acquire_lease(r, "sala-a", "w2", 5000)
    await bus.release_lease(r, "sala-a", "w1")
    assert await bus.acquire_lease(r, "sala-a", "w2", 5000)


async def test_lease_expires(r):
    assert await bus.acquire_lease(r, "sala-a", "w1", 100)
    await asyncio.sleep(0.2)
    assert await bus.acquire_lease(r, "sala-a", "w2", 100)


async def test_streams_finals_and_dedup(r):
    for c in [cap(0), cap(1, CaptionKind.PARTIAL), cap(1), cap(2)]:
        await bus.publish(r, c, "en")
    await bus.publish(r, cap(1), "en")  # a failover repeated seq 1
    finals = await bus.all_finals(r, "sala-a", "en")
    assert [c.seq for c in finals] == [0, 1, 2]
    last = await bus.last_finals(r, "sala-a", "en", 2)
    assert [m.seq for _, m in last] == [2, 1]  # oldest first: the repeated seq 1 is newest


async def test_fanout_replays_then_streams_live_and_resumes(r):
    await bus.save_session(r, cfg())
    for i in range(3):
        await bus.publish(r, cap(i), "en")
    fan = Fanout(r)
    got = []

    async def viewer(resume=None, n=5):
        out = []
        async with aclosing(fan.events("sala-a", "en", resume)) as events:
            async for e in events:
                if e is not None:
                    out.append(e)
                if len(out) == n:
                    return out

    task = asyncio.create_task(viewer())
    await asyncio.sleep(0.3)
    await bus.publish(r, cap(3, CaptionKind.PARTIAL), "en")
    await bus.publish(r, SessionEvent(session_id="sala-a", kind=SessionEventKind.ENDED), "en")
    got = await asyncio.wait_for(task, 5)
    kinds = [getattr(m, "kind", None) for _, m in got]
    assert [m.seq for _, m in got[:3]] == [0, 1, 2]  # replayed finals
    assert kinds[3] is CaptionKind.PARTIAL and kinds[4] is SessionEventKind.ENDED

    # resume after the 2nd entry: gets exactly what came after, no duplicates
    resumed = await asyncio.wait_for(viewer(resume=got[1][0], n=3), 5)
    assert [e[0] for e in resumed] == [e[0] for e in got[2:]]
    assert fan.viewers == {}  # hubs are cleaned up when viewers leave
    await fan.close()
