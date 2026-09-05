#!/usr/bin/env python3
"""
replay.py — Dark Track / ingestion: deterministic replay of stored ticks.

Re-emits raw_ticks rows for a time window in their original relative
timing (by default) so a session can be re-run for testing or demos
without hitting OpenSky or re-running radar_sim. Both sources come out
interleaved by observed_at, same as they'd arrive live.

    python3 replay.py 2026-09-05T14:00:00 2026-09-05T14:10:00
    python3 replay.py 2026-09-05T14:00:00 2026-09-05T14:10:00 --fast

Downstream consumers (fusion/) that want the raw feed rather than a
printed transcript should call db.ticks_in_window() directly instead of
shelling out to this script.
"""

import argparse
import time
from datetime import datetime, timezone

import db


def parse_ts(s):
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def run(start, end, fast, source):
    rows = db.ticks_in_window(start, end, source=source)
    if not rows:
        print("no ticks in that window")
        return

    print(f"replaying {len(rows)} ticks from {start} to {end} "
          f"({'fast' if fast else 'real-time'})\n")

    prev_observed_at = rows[0]["observed_at"]
    for row in rows:
        if not fast:
            gap = (row["observed_at"] - prev_observed_at).total_seconds()
            if gap > 0:
                time.sleep(gap)
        prev_observed_at = row["observed_at"]

        ts = row["observed_at"].strftime("%H:%M:%S")
        label = row["external_id"] or f"radar#{row['id']}"
        print(
            f"[{ts}] {row['source']:<5} {label:<10} "
            f"lat={row['lat']:.4f} lon={row['lon']:.4f} "
            f"alt={row['altitude_m']} vel={row['velocity_mps']}"
        )


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("start", type=parse_ts, help="ISO 8601 window start")
    p.add_argument("end", type=parse_ts, help="ISO 8601 window end")
    p.add_argument("--fast", action="store_true",
                    help="print immediately instead of sleeping between ticks")
    p.add_argument("--source", choices=["adsb", "radar"], default=None,
                    help="restrict to one source (default: both, interleaved)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.start, args.end, args.fast, args.source)
