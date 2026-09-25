"""
SSE load test: N simulated viewers spread over every live talk and language.

    uv run python loadtest/sse_clients.py --viewers 1500 --duration 120

Measures, per caption received live, the delivery delay:
    browser receive time - caption.emitted_at (set by the worker when it creates the caption)
That covers Valkey, the api fan-out, nginx and the network. It does not include
ASR/MT time, which the replay providers simulate and /api/sessions reports.

Worker and client clocks must agree: on one machine with Docker Desktop they do.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class Stats:
    connected: int = 0
    active: int = 0
    failed: int = 0  # never connected (HTTP error or refused)
    dropped: int = 0  # connected, then lost before the end
    events: int = 0
    delays: list[float] = field(default_factory=list)


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def raise_fd_limit(needed: int) -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = needed * 2 + 256
    if soft < want:
        new = want if hard == resource.RLIM_INFINITY else min(want, hard)
        resource.setrlimit(resource.RLIMIT_NOFILE, (new, hard))
        if new < want:
            print(f"warning: open-file limit is {new}, may be too low. Run `ulimit -n {want}` first.")


async def viewer(client: httpx.AsyncClient, url: str, st: Stats, stop: asyncio.Event) -> None:
    opened = time.time()
    connected = False
    try:
        async with client.stream("GET", url, headers={"Accept": "text/event-stream"}) as resp:
            if resp.status_code != 200:
                st.failed += 1
                return
            connected = True
            st.connected += 1
            st.active += 1
            event = None
            async for line in resp.aiter_lines():
                if stop.is_set():
                    return
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event == "caption":
                    now = time.time()
                    cap = json.loads(line[5:])
                    st.events += 1
                    if cap["emitted_at"] >= opened:  # skip the replayed backlog
                        st.delays.append(now - cap["emitted_at"])
                elif not line:
                    event = None
    except (httpx.HTTPError, OSError):
        if not connected:
            st.failed += 1
        elif not stop.is_set():
            st.dropped += 1
    finally:
        if connected:
            st.active -= 1


async def main() -> int:
    ap = argparse.ArgumentParser(description="conffy SSE load test")
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--viewers", type=int, default=1500)
    ap.add_argument("--duration", type=float, default=120, help="seconds after the ramp-up")
    ap.add_argument("--ramp", type=float, default=100, help="new viewers per second")
    ap.add_argument("--json", help="also write the summary to this file")
    args = ap.parse_args()
    raise_fd_limit(args.viewers)

    limits = httpx.Limits(max_connections=None, max_keepalive_connections=None)
    timeout = httpx.Timeout(connect=15, read=60, write=15, pool=None)  # keep-alives arrive every 15 s
    async with httpx.AsyncClient(base_url=args.base, limits=limits, timeout=timeout) as client:
        sessions = (await client.get("/api/sessions")).json()
        targets = [
            (s["config"]["id"], lang)
            for s in sessions
            if s["state"]["status"] in ("live", "waiting")
            for lang in [s["config"]["source_lang"], *s["config"]["target_langs"]]
        ]
        if not targets:
            print("no live talks found at", args.base)
            return 1
        talks = len({sid for sid, _ in targets})
        print(f"{args.viewers} viewers over {talks} talks / {len(targets)} caption streams, "
              f"ramp {args.ramp:.0f}/s, then {args.duration:.0f} s")

        st, stop = Stats(), asyncio.Event()
        tasks = []
        started = time.time()
        for i in range(args.viewers):
            sid, lang = targets[i % len(targets)]
            url = f"/api/sessions/{sid}/captions/{lang}/stream"
            tasks.append(asyncio.create_task(viewer(client, url, st, stop)))
            await asyncio.sleep(1 / args.ramp)
        ramp_s = time.time() - started

        measure_from = len(st.delays)
        events_at = st.events
        t0 = time.time()
        while (elapsed := time.time() - t0) < args.duration:
            await asyncio.sleep(5)
            window = st.delays[measure_from:]
            print(f"  {elapsed:5.0f}s  active={st.active:5d}  failed={st.failed}  dropped={st.dropped}  "
                  f"events/s={(st.events - events_at) / max(elapsed, 1):7.0f}  "
                  f"p50={pct(window, 50) * 1000:6.0f} ms  p95={pct(window, 95) * 1000:6.0f} ms")
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    window = st.delays[measure_from:]
    summary = {
        "viewers": args.viewers,
        "talks": talks,
        "streams": len(targets),
        "ramp_s": round(ramp_s, 1),
        "duration_s": args.duration,
        "connected": st.connected,
        "failed": st.failed,
        "dropped": st.dropped,
        "events_per_s": round((st.events - events_at) / args.duration, 1),
        "delay_ms": {
            "p50": round(pct(window, 50) * 1000, 1),
            "p95": round(pct(window, 95) * 1000, 1),
            "p99": round(pct(window, 99) * 1000, 1),
            "max": round(max(window, default=float("nan")) * 1000, 1),
            "mean": round(statistics.fmean(window) * 1000, 1) if window else None,
            "samples": len(window),
        },
    }
    print(json.dumps(summary, indent=2))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(summary, f, indent=2)
    return 0 if st.failed == 0 and st.dropped == 0 else 2


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)