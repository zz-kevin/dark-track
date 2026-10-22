#!/usr/bin/env python3
"""
bench.py — Dark Track / fusion: throughput + latency benchmark against
docs/PROBLEM.md's target:

    sustain fusion + anomaly detection for at least 150 concurrent tracks
    with end-to-end (sensor tick -> fused output) latency under 500 ms.

Replays a captured window through the live code path, cycle by cycle, at
full speed: the real fusion calls (simulate.py), the real persist() into
Postgres, and both real anomaly detectors over the result. To push past
the track count one window has, `--copies N` overlays N copies of the
window, each shifted 0.5 deg north (~55 km, far outside any gate) with
its own identities -- N times the concurrent traffic, same flight
behaviour.

A tick's end-to-end latency is the wait for the next fusion poll (it
arrives at its observed_at; fusion picks it up at the next poll) plus
that cycle's processing time, up to its row landing in fused_tracks.

Writes go to a scratch database (default `dark_track_bench`, created
from fusion/schema.sql if needed), never the live picture.

    python3 bench.py <start-iso> <end-iso> [--copies 4] [--poll-seconds 3]
"""

import argparse
import importlib.util
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

import db
import engine
import simulate

ROOT = Path(__file__).resolve().parent.parent


def _load_module(name, path):
    # ingestion/ and anomaly/ each have their own db.py.
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ingestion_db = _load_module("ingestion_db", ROOT / "ingestion" / "db.py")
detectors = _load_module("detectors", ROOT / "anomaly" / "detectors.py")
regions = _load_module("regions", ROOT / "ingestion" / "regions.py")
radar_model = _load_module("radar_model", ROOT / "ingestion" / "radar_model.py")


def overlay_copies(ticks, copies, shift_deg=0.5):
    """`copies` copies of the window, copy k shifted k*shift_deg north,
    with ids/callsigns made unique per copy."""
    out = []
    id_stride = max(t["id"] for t in ticks) + 1
    for k in range(copies):
        for t in ticks:
            out.append({
                **t,
                "id": t["id"] + k * id_stride,
                "lat": t["lat"] + k * shift_deg,
                "external_id": f"{t['external_id']}~{k}" if t["external_id"] and k else t["external_id"],
                "icao24": f"{t['icao24']}~{k}" if t.get("icao24") and k else t.get("icao24"),
            })
    return out


def ensure_bench_db(url):
    name = url.rsplit("/", 1)[-1]
    try:
        psycopg2.connect(url).close()
    except psycopg2.OperationalError:
        conn = psycopg2.connect("postgresql:///postgres")
        conn.autocommit = True
        conn.cursor().execute(f'CREATE DATABASE "{name}"')
        conn.close()
    db.DATABASE_URL = url
    db.init_schema()
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE fused_tracks, fused_track_events")


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))] if values else float("nan")


def bench(ticks, params, persist_to_db):
    sigma, consecutive = 4.0, 2
    silence, recency = detectors.went_dark_thresholds(params["adsb_interval_s"])
    kinematic_state = {}
    cycles, tick_latency = [], []
    gen = simulate.run(ticks, params)
    while True:
        t0 = time.perf_counter()
        try:
            cycle = next(gen)  # fusion: associate + filter updates + prune
        except StopIteration:
            break
        t1 = time.perf_counter()
        tracks, events = cycle["tracks"], cycle["events"]
        touched = {e["track_id"] for e in events}
        if persist_to_db and (events or cycle["retired"]):
            engine.persist(tracks, touched, events, cycle["retired"], cycle["poll_time"])
        t2 = time.perf_counter()
        snapshot = [
            {"track_id": t.id, "external_id": t.external_id, "adsb_hits": t.adsb_hits,
             "last_adsb_at": t.last_adsb_at, "last_radar_at": t.last_radar_at,
             "last_adsb_alt_m": t.last_adsb_alt_m}
            for t in tracks.values()
        ]
        detectors.went_dark(snapshot, cycle["poll_time"], silence, recency)
        _, _, kinematic_state = detectors.kinematic_inconsistency(
            events, kinematic_state, sigma, consecutive)
        t3 = time.perf_counter()

        fuse_s, persist_s, anomaly_s = t1 - t0, t2 - t1, t3 - t2
        cycles.append({"tracks": len(tracks), "ticks": len(cycle["batch"]),
                       "fuse": fuse_s, "persist": persist_s, "anomaly": anomaly_s})
        for row in cycle["batch"]:
            wait = max(0.0, (cycle["poll_time"] - row["observed_at"]).total_seconds())
            tick_latency.append(wait + fuse_s + persist_s)
    return cycles, tick_latency


def report(cycles, tick_latency, poll_seconds, copies):
    busy = [c for c in cycles if c["ticks"]]
    concurrent = [c["tracks"] for c in cycles]
    total = [c["fuse"] + c["persist"] + c["anomaly"] for c in busy]
    print(f"copies={copies} poll={poll_seconds}s: {len(cycles)} cycles, "
          f"concurrent tracks p50 {pct(concurrent, 0.5)} / max {max(concurrent)}, "
          f"{sum(c['ticks'] for c in cycles)} ticks")
    for key in ("fuse", "persist", "anomaly"):
        vals = [c[key] * 1000 for c in busy]
        print(f"  {key:<8} per cycle  p50 {pct(vals, .5):7.1f} ms  p95 {pct(vals, .95):7.1f} ms  "
              f"max {max(vals):7.1f} ms")
    vals = [v * 1000 for v in total]
    print(f"  {'total':<8} per cycle  p50 {pct(vals, .5):7.1f} ms  p95 {pct(vals, .95):7.1f} ms  "
          f"max {max(vals):7.1f} ms  ({max(total) / poll_seconds:.0%} of the poll budget)")
    lat = [v * 1000 for v in tick_latency]
    under = sum(1 for v in lat if v < 500) / len(lat)
    print(f"  tick -> fused_tracks  p50 {pct(lat, .5):7.1f} ms  p95 {pct(lat, .95):7.1f} ms  "
          f"max {max(lat):7.1f} ms  ({under:.1%} under 500 ms)")


def parse_ts(s):
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("start", type=parse_ts)
    p.add_argument("end", type=parse_ts)
    p.add_argument("--region", choices=sorted(regions.REGIONS), default=None)
    p.add_argument("--copies", type=int, default=1)
    p.add_argument("--radar", choices=["stored", "model"], default="stored",
                   help="model: regenerate the radar with ingestion/radar_model.py")
    p.add_argument("--poll-seconds", type=float, default=None,
                   help="fusion poll interval (default: engine.py's default)")
    p.add_argument("--no-db", action="store_true", help="skip the Postgres writes")
    p.add_argument("--bench-db", default=os.environ.get("BENCH_DATABASE_URL",
                                                         "postgresql:///dark_track_bench"))
    args = p.parse_args()

    try:
        ticks = regions.pick(ingestion_db.ticks_in_window(args.start, args.end), args.region)
    except ValueError as e:
        sys.exit(str(e))
    if not ticks:
        sys.exit("no ticks in that window")
    ticks = overlay_copies(ticks, args.copies)
    if args.radar == "model":  # after copying, so contact ids stay unique
        ticks = radar_model.regenerate(ticks, args.region)
    params = simulate.scaled_params(ticks)
    params["poll_seconds"] = args.poll_seconds or engine.DEFAULT_POLL_SECONDS
    if not args.no_db:
        ensure_bench_db(args.bench_db)
    report(*bench(ticks, params, not args.no_db), params["poll_seconds"], args.copies)


if __name__ == "__main__":
    main()
