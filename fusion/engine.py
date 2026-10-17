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
import importlib.util
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

import db
import geo
from associate import associate
from track import Track


def _load_regions():
    # ingestion/ has its own db.py, so load regions.py by path rather than
    # putting ingestion/ on sys.path.
    path = Path(__file__).resolve().parent.parent / "ingestion" / "regions.py"
    spec = importlib.util.spec_from_file_location("regions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


regions = _load_regions()


# A tick waits up to one poll for fusion to pick it up, so the poll sets the
# floor on docs/PROBLEM.md's 500 ms tick -> fused-output target. At the old
# 3s, bench.py measured 27% of ticks under 500 ms; at 0.25s, 100% (p95
# ~240 ms at 4x Gulf traffic). The two indexed SELECTs per poll are cheap.
DEFAULT_POLL_SECONDS = 0.25
# ADS-B ground speed + track fed to the filter as a velocity measurement,
# std dev in m/s per axis (None: position only, velocity just seeds new
# tracks). Pins the velocity a 45s prediction depends on -- see
# docs/FUSION.md "State estimation". Radar velocity is never measured: a
# real radar plot is a position, radar_sim's speed/heading only seed.
ADSB_VEL_STD_MPS = 10.0
VEL_SKIP_GROUND = True
VEL_TIME_OFFSET = False
# ADS-B position noise for the filter. Not the ~10 m a GPS fix is good to:
# OpenSky's position timestamps jitter by seconds (position-implied speed
# swings +/-20-30 m/s between fixes), which at 200+ m/s is ~50 m of
# effective error. 20 m made the filter trust that jitter; see
# docs/FUSION.md "State estimation".
R_ADSB_STD_M = 50.0
# Status line at most this often -- one per 0.25s poll would flood the log.
STATUS_EVERY_S = 3.0
# Longest wait between retries while the database is down.
DB_RETRY_MAX_S = 10.0

# Initial velocity variance when a track's starting velocity comes from the
# tick's reported speed/heading: ~(20 m/s)^2, vs. the (50 m/s)^2 default
# for an unknown starting velocity.
REPORTED_VEL_VAR = 400.0


def reported_velocity(row):
    """(vx, vy) m/s east/north from a raw tick's speed + heading, or None."""
    v, hdg = row.get("velocity_mps"), row.get("heading_deg")
    if v is None or hdg is None:
        return None
    rad = math.radians(hdg)
    return v * math.sin(rad), v * math.cos(rad)


def new_track(tick, accel_std, external_id=None):
    v0 = tick.get("v0")
    return Track(tick["x"], tick["y"], tick["observed_at"], tick["source"],
                 accel_std, tick["r_std"] ** 2, external_id=external_id, v0=v0,
                 init_vel_var=REPORTED_VEL_VAR if v0 is not None else 2500.0)


def tick_from_row(row, source, r_std):
    """A raw_ticks row -> the tick dict apply_identified/apply_anonymous
    take. Shared with simulate.py, so replays fuse exactly what live does."""
    x, y = geo.to_local(row["lat"], row["lon"])
    return {
        "raw_id": row["id"],
        "external_id": row["external_id"],
        "x": x,
        "y": y,
        "observed_at": row["observed_at"],
        "r_std": r_std,
        "source": source,
        "aircraft_type": row.get("aircraft_type"),
        "aircraft_variant": row.get("aircraft_variant"),
        "v0": reported_velocity(row),
        "altitude_m": row.get("altitude_m"),
        # How much newer the reported velocity is than the position fix
        # (0 when unknown -- rows from before velocity_at was recorded).
        "velocity_lead_s": (
            (row["velocity_at"] - row["observed_at"]).total_seconds()
            if row.get("velocity_at") is not None else 0.0
        ),
    }


def load_new_ticks(source, cursor, r_std, bbox=None):
    rows = db.raw_ticks_since(source, cursor, bbox)
    ticks = [tick_from_row(row, source, r_std) for row in rows]
    new_cursor = max((row["id"] for row in rows), default=cursor)
    return ticks, new_cursor


def _region_bbox(region):
    """Bounding box for --region, plus a warning when none was given but
    recent data comes from more than one region: fusion projects everything
    onto one flat-earth plane centered on the data, which is only valid
    for one region at a time (ingestion/regions.py)."""
    if region is not None:
        lamin, lamax, lomin, lomax = regions.REGIONS[region]
        m = regions.MARGIN_DEG
        return (lamin - m, lamax + m, lomin - m, lomax + m)
    live = {regions.region_of(lat, lon) for lat, lon in db.recent_points()}
    if len(live) > 1:
        print(f"WARNING: recent ADS-B comes from more than one region "
              f"({', '.join(str(r) for r in live)}). Run one fusion engine per "
              f"region with --region, or distances will be off.")
    return None


def _record_update(events, touched, track, tick, created_new_track):
    events.append({
        "track_id": track.id,
        "source": tick["source"],
        "raw_tick_id": tick["raw_id"],
        "observed_at": tick["observed_at"],
        "residual_m": track.last_residual_m,
        "innovation_std_m": track.last_innovation_std_m,
        "created_new_track": created_new_track,
        # Fused position right after this update -- not stored, but lets
        # fusion/accuracy.py score the estimate at the tick's own time.
        "fused_xy": track.kf.position,
    })
    touched.add(track.id)


def _velocity_measurement(tick, accel_std):
    """(v, rv_std) for Track.apply: the reported velocity of an ADS-B tick,
    or (None, None). The velocity is from the aircraft's latest message,
    often seconds after its position fix; under the filter's own
    white-noise-acceleration model it has drifted by ~accel_std * gap since
    the fix, so that's added to its noise. Measured on NYC approach turns:
    reported heading led the position-implied one through every turn."""
    if tick["source"] != "adsb" or ADSB_VEL_STD_MPS is None or tick.get("v0") is None:
        return None, None
    # On the ground (OpenSky reports no barometric altitude), a taxiing
    # aircraft's reported track is unreliable at walking-pace speeds.
    if VEL_SKIP_GROUND and tick.get("altitude_m") is None:
        return None, None
    lead = abs(tick.get("velocity_lead_s", 0.0)) if VEL_TIME_OFFSET else 0.0
    return tick["v0"], math.hypot(ADSB_VEL_STD_MPS, accel_std * lead)


def apply_identified(ticks, tracks, callsign_to_track, accel_std, events, touched):
    """ADS-B ticks with a callsign: unambiguous, update-or-create by identity."""
    for tick in sorted(ticks, key=lambda t: t["observed_at"]):
        tid = callsign_to_track.get(tick["external_id"])
        track = tracks.get(tid)
        if track is not None:
            track.apply(tick["x"], tick["y"], tick["observed_at"], "adsb", tick["r_std"],
                        *_velocity_measurement(tick, accel_std))
            track.note_type(tick.get("aircraft_type"), tick.get("aircraft_variant"))
            track.last_adsb_alt_m = tick.get("altitude_m")
            _record_update(events, touched, track, tick, created_new_track=False)
        else:
            track = new_track(tick, accel_std, external_id=tick["external_id"])
            track.note_type(tick.get("aircraft_type"), tick.get("aircraft_variant"))
            track.last_adsb_alt_m = tick.get("altitude_m")
            tracks[track.id] = track
            callsign_to_track[tick["external_id"]] = track.id
            _record_update(events, touched, track, tick, created_new_track=True)


# Anonymous ticks are associated in time-ordered slices this wide, not as
# one poll-cycle batch. Hungarian assignment is one-to-one, so two contacts
# from the same aircraft in one batch can't both join its track -- the
# loser spawned a phantom track. Measured on NYC (11s cadence, 8% of
# revisits <= 3s apart, i.e. inside one 3s poll): 89 of 110 airborne
# contacts that missed their own track lost it to the same aircraft's
# other contact. Same-aircraft contacts under 1s apart are rare, and
# slicing also feeds each filter its contacts in time order.
ASSOCIATION_SLICE_S = 1.0


def apply_anonymous(ticks, tracks, accel_std, gate_dist_m, gate_staleness_s,
                     events, touched):
    """Radar contacts and blank-callsign ADS-B ticks: no identity, so gate
    + Hungarian-assign them against the current track set, one
    ASSOCIATION_SLICE_S slice of the batch at a time."""
    ticks = sorted(ticks, key=lambda t: t["observed_at"])
    while ticks:
        cut = 1
        while (cut < len(ticks) and (ticks[cut]["observed_at"] - ticks[0]["observed_at"])
               .total_seconds() < ASSOCIATION_SLICE_S):
            cut += 1
        _apply_anonymous_slice(ticks[:cut], tracks, accel_std, gate_dist_m,
                               gate_staleness_s, events, touched)
        ticks = ticks[cut:]


def _apply_anonymous_slice(ticks, tracks, accel_std, gate_dist_m, gate_staleness_s,
                           events, touched):
    matches = associate(ticks, tracks, gate_dist_m, gate_staleness_s)
    for tick, tid in zip(ticks, matches):
        track = tracks.get(tid)
        if track is not None:
            track.apply(tick["x"], tick["y"], tick["observed_at"],
                        tick["source"], tick["r_std"], *_velocity_measurement(tick, accel_std))
            _record_update(events, touched, track, tick, created_new_track=False)
        else:
            track = new_track(tick, accel_std)
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
    return stale_ids


def track_row(track, now):
    lat, lon = geo.to_latlon(*track.kf.position)
    return {
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
        "aircraft_type": track.aircraft_type,
        "aircraft_variant": track.aircraft_variant,
        "last_adsb_alt_m": track.last_adsb_alt_m,
    }


def persist(tracks, touched_ids, events, retired_ids, now):
    """One cycle's output -- event log, touched tracks, retired tracks --
    in one transaction. Was one connection per touched track: measured
    p95 759 ms per cycle on a 45s-cadence Gulf burst (~120 tracks at once),
    more than the whole 500 ms latency target on its own."""
    rows = [track_row(tracks[tid], now) for tid in touched_ids if tid in tracks]
    db.write_cycle(events, rows, retired_ids)


def scaled_gates(interval_s):
    """Staleness gate and track TTL for a given ADS-B update interval. The
    gate has to outlast one full sensor revisit: when a transponder drops,
    the next radar contact arrives ~one interval after the track's last
    update, and a shorter gate turns it into a new anonymous track instead
    of the went-dark track it belongs to.

    2.5 intervals covers one revisit, one *dropped* revisit (radar_sim
    drops 10% of contacts) and latency jitter. At 2 intervals, a single
    dropped contact put the next one just past the gate -- 94s after the
    last update against a 90s gate on Gulf data -- and the dark track lost
    its radar for good (most of the held-out went-dark misses). Gating for
    the whole TTL fixed Gulf too, but at NYC's 30s floor -> 120s it let
    silent tracks pick up neighbours' contacts: 0% -> 4% false went-dark.
    Floors are the values tuned for ~10s polling, so NYC is unchanged."""
    return max(30.0, 2.5 * interval_s), max(120.0, 3.0 * interval_s)


def run(poll_seconds, gate_dist_m, gate_staleness_s, track_ttl_s,
        r_adsb_std_m, r_radar_std_m, accel_std, since_id_adsb, since_id_radar,
        region=None):
    bbox = _region_bbox(region)
    cursor_adsb = since_id_adsb if since_id_adsb is not None else db.latest_raw_id("adsb")
    cursor_radar = since_id_radar if since_id_radar is not None else db.latest_raw_id("radar")
    interval = db.adsb_update_interval()
    if gate_staleness_s is None or track_ttl_s is None:
        auto_gate, auto_ttl = scaled_gates(interval or 10.0)
        gate_staleness_s = gate_staleness_s if gate_staleness_s is not None else auto_gate
        track_ttl_s = track_ttl_s if track_ttl_s is not None else auto_ttl
    print(f"adsb update interval: "
          f"{f'{interval:.0f}s (measured)' if interval else 'unknown, assuming 10s'}")
    center = db.recent_center(bbox=bbox)
    if center:
        geo.set_reference(*center)
        print(f"flat-earth reference: {center[0]:.3f}, {center[1]:.3f} "
              f"(center of recent adsb data)")
    tracks = {}
    callsign_to_track = {}
    Track._next_id = db.next_track_id()
    stale = db.clear_fused_tracks()
    if stale:
        print(f"cleared {stale} stale tracks left by a previous run "
              f"(history kept in fused_track_events)")

    print(
        f"Fusing adsb ticks after id={cursor_adsb}, radar ticks after "
        f"id={cursor_radar}, every {poll_seconds}s "
        f"(gate={gate_dist_m}m/{gate_staleness_s}s, ttl={track_ttl_s}s). "
        f"Ctrl+C to stop.\n"
    )

    pending = {"adsb": 0, "radar": 0, "touched": set(), "retired": 0}
    last_status = 0.0
    # Output fused but not yet written -- kept across a database outage so
    # the event log and fused_tracks catch up once Postgres is back.
    unwritten = {"events": [], "touched": set(), "retired": []}
    db_down_since = None

    while True:
        try:
            adsb_ticks, cursor_adsb = load_new_ticks("adsb", cursor_adsb, r_adsb_std_m, bbox)
            radar_ticks, cursor_radar = load_new_ticks("radar", cursor_radar, r_radar_std_m, bbox)

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
            unwritten["events"] += events
            unwritten["touched"] |= touched
            unwritten["retired"] += stale_ids
            if unwritten["events"] or unwritten["touched"] or unwritten["retired"]:
                persist(tracks, unwritten["touched"], unwritten["events"],
                        unwritten["retired"], now)
                unwritten = {"events": [], "touched": set(), "retired": []}
            if db_down_since is not None:
                print(f"database back after {time.time() - db_down_since:.0f}s -- caught up")
                db_down_since = None

            pending["adsb"] += len(adsb_ticks)
            pending["radar"] += len(radar_ticks)
            pending["touched"] |= touched
            pending["retired"] += len(stale_ids)
            if ((pending["adsb"] or pending["radar"] or pending["retired"])
                    and time.time() - last_status >= STATUS_EVERY_S):
                # Ticks come back from Postgres in the session's local zone,
                # the idle-cycle fallback is UTC -- print both as UTC, same
                # as the poller.
                print(
                    f"[{now.astimezone(timezone.utc).strftime('%H:%M:%S')}] "
                    f"+{pending['adsb']} adsb "
                    f"+{pending['radar']} radar -> {len(pending['touched'])} tracks touched, "
                    f"{pending['retired']} retired, {len(tracks)} active"
                )
                pending = {"adsb": 0, "radar": 0, "touched": set(), "retired": 0}
                last_status = time.time()
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            # Postgres restarting or unreachable: keep the in-memory tracks
            # and retry. Reads that failed consumed nothing (cursors only
            # advance on success); fused output waits in `unwritten`.
            if db_down_since is None:
                db_down_since = time.time()
                print(f"database unavailable ({str(e).strip().splitlines()[0]}) -- "
                      f"keeping {len(tracks)} tracks in memory, retrying")
            time.sleep(min(DB_RETRY_MAX_S, max(poll_seconds, time.time() - db_down_since)))
            continue
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
    p.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    p.add_argument("--gate-dist-m", type=float, default=800.0,
                    help="max distance between a track's predicted position "
                         "and a tick for them to be association candidates")
    p.add_argument("--gate-staleness-s", type=float, default=None,
                    help="don't gate against a track not updated within this long "
                         "(default: scaled to the measured ADS-B interval, min 30s)")
    p.add_argument("--track-ttl-s", type=float, default=None,
                    help="retire a track after this long with no update "
                         "(default: scaled to the measured ADS-B interval, min 120s)")
    p.add_argument("--r-adsb-std-m", type=float, default=R_ADSB_STD_M,
                    help="assumed ADS-B position measurement noise, meters")
    p.add_argument("--r-radar-std-m", type=float, default=150.0,
                    help="assumed radar position measurement noise, meters "
                         "-- should match radar_sim.py's --noise-std-m")
    p.add_argument("--adsb-vel-std", type=float, default=ADSB_VEL_STD_MPS,
                    help="ADS-B reported velocity noise, m/s per axis, when used "
                         "as a filter measurement (0: position only)")
    p.add_argument("--accel-std", type=float, default=3.0,
                    help="process noise: expected accel, m/s^2")
    p.add_argument("--region", choices=sorted(regions.REGIONS), default=None,
                    help="fuse only this region's ticks (needed when two pollers "
                         "run at once; see ingestion/regions.py)")
    p.add_argument("--since-id-adsb", type=int, default=None)
    p.add_argument("--since-id-radar", type=int, default=None)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    ADSB_VEL_STD_MPS = args.adsb_vel_std or None
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
        region=args.region,
    )
