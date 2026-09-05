#!/usr/bin/env python3
"""
engine.py — Dark Track / fusion: main fusion loop.

Turns the two raw_ticks streams (source='adsb', source='radar') into one
set of fused tracks, each backed by a Kalman filter (kalman.py). Run this
alongside ingestion/opensky_poll.py and ingestion/radar_sim.py (same
DATABASE_URL). Run `python3 db.py` once first to create the fused_tracks /
fused_track_events tables.

Each poll cycle, in order:

  1. ADS-B ticks that carry a callsign update-or-create their track
     directly by callsign -- no ambiguity, so no gating needed.
  2. Every other new tick (radar contacts, plus the rare blank-callsign
     ADS-B tick) is anonymous: gated against the resulting track set and
     assigned via the Hungarian algorithm in one batch (associate.py).
     Unmatched anonymous ticks spawn new tracks -- a genuinely new radar
     contact, or noise.
  3. Tracks with no update in --track-ttl-s are retired.

This is a documented simplification, not a fully time-ordered merge of
both streams -- see docs/FUSION.md "Conflict resolution".
"""

import argparse
import time
from datetime import datetime, timezone

import db
import geo
from associate import associate
from track import Track


def load_new_ticks(source, cursor, r_std):
    rows = db.raw_ticks_since(source, cursor)
    ticks = []
    new_cursor = cursor
    for row in rows:
        x, y = geo.to_local(row["lat"], row["lon"])
        ticks.append({
            "raw_id": row["id"],
            "external_id": row["external_id"],
            "x": x,
            "y": y,
            "observed_at": row["observed_at"],
            "r_std": r_std,
            "source": source,
        })
        new_cursor = max(new_cursor, row["id"])
    return ticks, new_cursor


def _record_update(events, touched, track, tick, created_new_track):
    events.append({
        "track_id": track.id,
        "source": tick["source"],
        "raw_tick_id": tick["raw_id"],
        "observed_at": tick["observed_at"],
        "residual_m": track.last_residual_m,
        "created_new_track": created_new_track,
    })
    touched.add(track.id)


def apply_identified(ticks, tracks, callsign_to_track, accel_std, events, touched):
    """ADS-B ticks with a callsign: unambiguous, update-or-create by identity."""
    for tick in sorted(ticks, key=lambda t: t["observed_at"]):
        tid = callsign_to_track.get(tick["external_id"])
        track = tracks.get(tid)
        if track is not None:
            track.apply(tick["x"], tick["y"], tick["observed_at"], "adsb", tick["r_std"])
            _record_update(events, touched, track, tick, created_new_track=False)
        else:
            track = Track(tick["x"], tick["y"], tick["observed_at"], "adsb",
                           accel_std, tick["r_std"] ** 2, external_id=tick["external_id"])
            tracks[track.id] = track
            callsign_to_track[tick["external_id"]] = track.id
            _record_update(events, touched, track, tick, created_new_track=True)


def apply_anonymous(ticks, tracks, accel_std, gate_dist_m, gate_staleness_s,
                     events, touched):
    """Radar contacts and blank-callsign ADS-B ticks: no identity, so gate
    + Hungarian-assign the whole batch against the current track set."""
    matches = associate(ticks, tracks, gate_dist_m, gate_staleness_s)
    for tick, tid in zip(ticks, matches):
        track = tracks.get(tid)
        if track is not None:
            track.apply(tick["x"], tick["y"], tick["observed_at"],
                        tick["source"], tick["r_std"])
            _record_update(events, touched, track, tick, created_new_track=False)
        else:
            track = Track(tick["x"], tick["y"], tick["observed_at"],
                          tick["source"], accel_std, tick["r_std"] ** 2)
            tracks[track.id] = track
            _record_update(events, touched, track, tick, created_new_track=True)


def prune_stale(tracks, callsign_to_track, now, track_ttl_s):
    stale_ids = [
        tid for tid, t in tracks.items()
        if (now - t.last_update).total_seconds() > track_ttl_s
    ]
    for tid in stale_ids:
        track = tracks.pop(tid)
        if track.external_id and callsign_to_track.get(track.external_id) == tid:
            del callsign_to_track[track.external_id]
        db.delete_fused_track(tid)
    return stale_ids


def persist(tracks, touched_ids, now):
    for tid in touched_ids:
        track = tracks.get(tid)
        if track is None:
            continue
        lat, lon = geo.to_latlon(*track.kf.position)
        db.upsert_fused_track({
            "track_id": track.id,
            "external_id": track.external_id,
            "lat": lat,
            "lon": lon,
            "speed_mps": track.speed_mps(),
            "heading_deg": track.heading_deg(),
            "confidence": track.confidence(now),
            "adsb_hits": track.adsb_hits,
            "radar_hits": track.radar_hits,
            "last_adsb_at": track.last_adsb_at,
            "last_radar_at": track.last_radar_at,
            "created_at": track.created_at,
            "updated_at": now,
        })


def run(poll_seconds, gate_dist_m, gate_staleness_s, track_ttl_s,
        r_adsb_std_m, r_radar_std_m, accel_std, since_id_adsb, since_id_radar):
    cursor_adsb = since_id_adsb if since_id_adsb is not None else db.latest_raw_id("adsb")
    cursor_radar = since_id_radar if since_id_radar is not None else db.latest_raw_id("radar")
    tracks = {}
    callsign_to_track = {}

    print(
        f"Fusing adsb ticks after id={cursor_adsb}, radar ticks after "
        f"id={cursor_radar}, every {poll_seconds}s "
        f"(gate={gate_dist_m}m/{gate_staleness_s}s, ttl={track_ttl_s}s). "
        f"Ctrl+C to stop.\n"
    )

    while True:
        try:
            adsb_ticks, cursor_adsb = load_new_ticks("adsb", cursor_adsb, r_adsb_std_m)
            radar_ticks, cursor_radar = load_new_ticks("radar", cursor_radar, r_radar_std_m)

            identified = [t for t in adsb_ticks if t["external_id"] is not None]
            anonymous = [t for t in adsb_ticks if t["external_id"] is None] + radar_ticks

            events = []
            touched = set()

            if identified:
                apply_identified(identified, tracks, callsign_to_track, accel_std,
                                  events, touched)
            if anonymous:
                apply_anonymous(anonymous, tracks, accel_std, gate_dist_m,
                                 gate_staleness_s, events, touched)

            now = max(
                (t["observed_at"] for t in adsb_ticks + radar_ticks),
                default=datetime.now(timezone.utc),
            )
            stale_ids = prune_stale(tracks, callsign_to_track, now, track_ttl_s)

            if events:
                db.insert_fused_events(events)
            if touched:
                persist(tracks, touched, now)

            if adsb_ticks or radar_ticks or stale_ids:
                print(
                    f"[{now.strftime('%H:%M:%S')}] +{len(adsb_ticks)} adsb "
                    f"+{len(radar_ticks)} radar -> {len(touched)} tracks touched, "
                    f"{len(stale_ids)} retired, {len(tracks)} active"
                )
        except Exception as e:
            print(f"fusion tick failed: {e}")
            raise

        try:
            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("\nstopped.")
            break


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--poll-seconds", type=float, default=3.0)
    p.add_argument("--gate-dist-m", type=float, default=800.0,
                    help="max distance between a track's predicted position "
                         "and a tick for them to be association candidates")
    p.add_argument("--gate-staleness-s", type=float, default=30.0,
                    help="don't gate against a track not updated within this long")
    p.add_argument("--track-ttl-s", type=float, default=120.0,
                    help="retire a track after this long with no update")
    p.add_argument("--r-adsb-std-m", type=float, default=20.0,
                    help="assumed ADS-B position measurement noise, meters")
    p.add_argument("--r-radar-std-m", type=float, default=150.0,
                    help="assumed radar position measurement noise, meters "
                         "-- should match radar_sim.py's --noise-std-m")
    p.add_argument("--accel-std", type=float, default=3.0,
                    help="process noise: expected accel, m/s^2")
    p.add_argument("--since-id-adsb", type=int, default=None)
    p.add_argument("--since-id-radar", type=int, default=None)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(
        poll_seconds=args.poll_seconds,
        gate_dist_m=args.gate_dist_m,
        gate_staleness_s=args.gate_staleness_s,
        track_ttl_s=args.track_ttl_s,
        r_adsb_std_m=args.r_adsb_std_m,
        r_radar_std_m=args.r_radar_std_m,
        accel_std=args.accel_std,
        since_id_adsb=args.since_id_adsb,
        since_id_radar=args.since_id_radar,
    )
