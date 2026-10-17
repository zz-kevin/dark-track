"""
simulate.py — Dark Track / fusion: run the real fusion engine in memory
over an already-captured window of raw_ticks.

Same apply_identified / apply_anonymous / prune_stale calls as the live
loop in engine.py, advancing in simulated poll cycles instead of sleeping,
with no database writes. Shared by everything that needs to re-run fusion
against stored data: anomaly/harness.py (precision/recall), accuracy.py
(association accuracy), bench.py (throughput), and api/'s replay
endpoint.
"""

import random
from collections import defaultdict
from datetime import timedelta

import engine
import geo
from track import Track

DEFAULT_PARAMS = dict(
    # Live fusion polls every 0.25s (engine.DEFAULT_POLL_SECONDS), but
    # anomaly/engine.py checks the picture every 3s, and that's the cadence
    # detection latency depends on -- so replays step at 3s. Association
    # runs in 1s slices either way (engine.ASSOCIATION_SLICE_S), so the
    # coarser step doesn't change who gets matched to what.
    poll_seconds=3.0,
    accel_std=3.0,
    gate_dist_m=800.0,
    gate_staleness_s=30.0,
    track_ttl_s=120.0,
    r_adsb_std_m=engine.R_ADSB_STD_M,
    # Height error for radar contacts stored with the ADS-B altitude copied
    # exactly (before radar_sim simulated height error), added at read time.
    radar_height_std_m=300.0,
    r_radar_std_m=150.0,
)


def poll_cycles(ticks, poll_seconds):
    """Buckets `ticks` into fixed poll_seconds-wide windows spanning the
    data, same mental model as engine.py's live poll loop: at each poll,
    whatever arrived since the last poll gets processed as one batch.
    Yields (poll_time, batch) in order, including empty batches (a live
    poller ticks on a timer regardless of whether new data arrived, and
    anomaly/engine.py's own poll cadence matters for detection latency)."""
    if not ticks:
        return
    start = min(t["observed_at"] for t in ticks)
    end = max(t["observed_at"] for t in ticks)

    buckets = defaultdict(list)
    for t in ticks:
        buckets[t["observed_at"]].append(t)

    poll_time = start
    step = timedelta(seconds=poll_seconds)
    pending = []
    sorted_times = sorted(buckets)
    ti = 0
    while poll_time <= end:
        while ti < len(sorted_times) and sorted_times[ti] <= poll_time:
            pending.extend(buckets[sorted_times[ti]])
            ti += 1
        yield poll_time, pending
        pending = []
        poll_time += step
    # anything left after the last full poll_seconds step (fractional tail)
    if ti < len(sorted_times):
        for t in sorted_times[ti:]:
            pending.extend(buckets[t])
        yield poll_time, pending


def set_reference_from(ticks):
    """Same flat-earth reference the live engine would pick: the center of
    the data, not geo.py's NYC default."""
    if ticks:
        geo.set_reference(
            sum(t["lat"] for t in ticks) / len(ticks),
            sum(t["lon"] for t in ticks) / len(ticks),
        )


def _with_radar_height_error(row, std):
    """A radar row stored with the ADS-B altitude copied exactly gets the
    height error radar_sim now adds, seeded by row id so every replay of
    a window sees the same contacts."""
    if row["source"] != "radar" or row["altitude_m"] is None or row.get("altitude_std_m"):
        return row
    rng = random.Random(row["id"])
    return {**row, "altitude_m": row["altitude_m"] + rng.gauss(0, std), "altitude_std_m": std}


def run(ticks, params=None):
    """Generator: fuses `ticks` (raw_ticks rows, both sources) cycle by
    cycle. Yields one dict per poll cycle:

        poll_time, tracks (track_id -> Track, live object -- read it before
        advancing the generator), events (this cycle's fused_track_events
        rows, plus fused_xy), retired (track ids pruned this cycle),
        batch (the raw rows processed this cycle).
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    set_reference_from(ticks)
    Track._next_id = 1
    tracks = {}
    callsign_to_track = {}

    for poll_time, batch in poll_cycles(ticks, p["poll_seconds"]):
        identified, anonymous = [], []
        for row in batch:
            row = _with_radar_height_error(row, p["radar_height_std_m"])
            r_std = p["r_adsb_std_m"] if row["source"] == "adsb" else p["r_radar_std_m"]
            tick = engine.tick_from_row(row, row["source"], r_std)
            if row["source"] == "adsb" and row["external_id"] is not None:
                identified.append(tick)
            else:
                anonymous.append(tick)

        events, touched = [], set()
        if identified:
            engine.apply_identified(identified, tracks, callsign_to_track,
                                    p["accel_std"], events, touched)
        if anonymous:
            engine.apply_anonymous(anonymous, tracks, p["accel_std"], p["gate_dist_m"],
                                   p["gate_staleness_s"], events, touched)
        retired = engine.prune_stale(tracks, callsign_to_track, poll_time, p["track_ttl_s"])

        yield {"poll_time": poll_time, "tracks": tracks, "events": events,
               "retired": retired, "batch": batch}


def window_interval(ticks):
    """Median per-aircraft ADS-B revisit interval within the window -- the
    same quantity the live engines measure from the database
    (db.adsb_update_interval)."""
    last, gaps = {}, []
    for t in sorted((t for t in ticks if t["source"] == "adsb"), key=lambda t: t["observed_at"]):
        key = t.get("icao24") or t["external_id"]
        if key in last:
            gap = (t["observed_at"] - last[key]).total_seconds()
            if 0 < gap < 300:
                gaps.append(gap)
        last[key] = t["observed_at"]
    return sorted(gaps)[len(gaps) // 2] if gaps else None


def scaled_params(ticks):
    """DEFAULT_PARAMS with the staleness gate and TTL scaled to the
    window's own ADS-B cadence, as the live engine does at startup."""
    interval = window_interval(ticks) or 10.0
    gate, ttl = engine.scaled_gates(interval)
    return {**DEFAULT_PARAMS, "gate_staleness_s": gate, "track_ttl_s": ttl,
            "adsb_interval_s": interval}
