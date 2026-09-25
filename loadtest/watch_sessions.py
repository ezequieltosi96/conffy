"""
Prints every few seconds the latencies each worker reports for its talk.
Use it to find how many live talks one machine can handle with real models:
the machine is saturated when `lag` keeps growing instead of staying ~1 s.

    uv run python loadtest/watch_sessions.py            # every 5 s, Ctrl+C to stop
    uv run python loadtest/watch_sessions.py --csv capacity.csv
"""

from __future__ import annotations

import argparse
import csv
import statistics
import time

import httpx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--every", type=float, default=5)
    ap.add_argument("--csv", help="append every sample to this CSV file")
    args = ap.parse_args()
    writer = None
    if args.csv:
        f = open(args.csv, "a", newline="")
        writer = csv.writer(f)
        if f.tell() == 0:
            writer.writerow(["time", "session", "status", "asr_ms", "mt_ms", "lag_s", "last_caption_age_s"])

    while True:
        now = time.time()
        rows = httpx.get(f"{args.base}/api/sessions", timeout=10).json()
        live = [r for r in rows if r["state"]["status"] == "live"]
        print(f"\n{time.strftime('%H:%M:%S')}  live talks: {len(live)}/{len(rows)}")
        print(f"  {'session':<14}{'asr ms':>8}{'mt ms':>8}{'lag s':>7}{'last caption':>14}")
        for r in rows:
            s = r["state"]
            age = now - s["last_caption_at"] if s.get("last_caption_at") else None
            print(f"  {r['config']['id']:<14}{s['asr_ms'] or 0:>8.0f}{s['mt_ms'] or 0:>8.0f}"
                  f"{s['audio_lag_s']:>7.1f}{(f'{age:.0f} s ago' if age is not None else '-'):>14}"
                  f"   {s['status']}")
            if writer:
                writer.writerow([round(now), r["config"]["id"], s["status"], s["asr_ms"], s["mt_ms"],
                                 s["audio_lag_s"], round(age, 1) if age is not None else ""])
        if live:
            lags = [r["state"]["audio_lag_s"] for r in live]
            asr = [r["state"]["asr_ms"] or 0 for r in live]
            print(f"  avg lag {statistics.fmean(lags):.1f} s (max {max(lags):.1f})   "
                  f"avg asr {statistics.fmean(asr):.0f} ms")
        if writer:
            f.flush()
        time.sleep(args.every)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass