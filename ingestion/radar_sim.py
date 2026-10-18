#!/usr/bin/env python3
"""
radar_sim.py — Dark Track / ingestion: synthetic non-cooperative radar
contact generator.

Reads real ADS-B ticks (source='adsb') already written to `raw_ticks` by
opensky_poll.py, and derives a second, degraded feed from them:

  - Gaussian position noise (radar-like measurement error)
  - Latency jitter (contact appears some seconds after the real position
    fix, modeling sensor-to-track processing delay)
  - Random dropout (the "radar" simply doesn't detect this tick at all)
  - No ID field (external_id is always NULL) — this is the asymmetry that
    forces real gating/association logic in fusion/, since the two sources
    can't just be joined on an identifier.

Run this alongside opensky_poll.py (same DATABASE_URL). It only reads
adsb ticks that arrive *after* it starts, so run opensky_poll.py first.

--model realistic swaps the fixed noise and dropout for radar_model.py's
radar: a site per region, accuracy that degrades with range, detection
that falls off with range and below the horizon, and clutter. Each
contact then records its own accuracy (noise_std_m) for fusion.

Latency and dropout are simulated by writing `observed_at` in the future
relative to the source tick's true time and by seeding the RNG, rather than
literally sleeping per-tick — this keeps a replay of a fixed --seed run
deterministic.
"""

import argparse
import math
import random
import sys
import time
from datetime import timedelta

import db
import radar_model

EARTH_RADIUS_M = 6371000
METERS_PER_DEG_LAT = 111320


def jitter_position(lat, lon, noise_std_m, rng):
    """Add independent Gaussian noise (meters) to lat/lon."""
    dn = rng.gauss(0, noise_std_m)  # north/south offset, meters
    de = rng.gauss(0, noise_std_m)  # east/west offset, meters
    dlat = dn / METERS_PER_DEG_LAT
    dlon = de / (METERS_PER_DEG_LAT * math.cos(math.radians(lat)) or 1e-9)
    return lat + dlat, lon + dlon


def advance(lat, lon, velocity, heading, seconds):
    """Dead-reckon a position forward along the reported track."""
    if velocity is None or heading is None:
        return lat, lon
    dist = velocity * seconds
    dn = dist * math.cos(math.radians(heading))
    de = dist * math.sin(math.radians(heading))
    dlat = dn / METERS_PER_DEG_LAT
    dlon = de / (METERS_PER_DEG_LAT * math.cos(math.radians(lat)) or 1e-9)
    return lat + dlat, lon + dlon


def make_radar_row(adsb_row, noise_std_m, latency_min, latency_max, rng,
                   height_noise_std_m=300.0):
    latency = rng.uniform(latency_min, latency_max)
    velocity = adsb_row["velocity_mps"]
    heading = adsb_row["heading_deg"]
    # observed_at is shifted forward by `latency`, so the position has to be
    # where the aircraft is at that shifted time -- not where it was at the
    # ADS-B fix. Otherwise a 230 m/s airliner's contact lands ~1km behind
    # its own track and misses fusion's 800m gate.
    lat, lon = advance(adsb_row["lat"], adsb_row["lon"], velocity, heading, latency)
    lat, lon = jitter_position(lat, lon, noise_std_m, rng)
    return {
        "source": "radar",
        "external_id": None,  # no ID -- the whole point of this source
        "lat": lat,
        "lon": lon,
        # A 3D surveillance radar measures height, coarsely. (Until
        # 2026-10-16 this copied the ADS-B altitude exactly.)
        "altitude_m": (adsb_row["altitude_m"] + rng.gauss(0, height_noise_std_m)
                       if adsb_row["altitude_m"] is not None else None),
        "altitude_std_m": height_noise_std_m if adsb_row["altitude_m"] is not None else None,
        "velocity_mps": velocity + rng.gauss(0, 2.0) if velocity is not None else None,
        "heading_deg": heading + rng.gauss(0, 3.0) if heading is not None else None,
        "observed_at": adsb_row["observed_at"] + timedelta(seconds=latency),
        # Ground truth for scoring only (see ingestion/radar_truth.py);
        # fusion never reads it.
        "truth_tick_id": adsb_row["id"],
    }


def realistic_contacts(rows, site, rng, clutter_from):
    """radar_model contacts for `rows`, plus clutter from clutter_from (the
    previous batch's last fix time) up to this batch's last. Returns
    (contacts, undetected, last fix time)."""
    contacts = [radar_model.contact(site, row, rng) for row in rows]
    detected = [c for c in contacts if c is not None]
    last = max(row["observed_at"] for row in rows)
    if clutter_from is not None and last > clutter_from:
        detected += radar_model.clutter(site, clutter_from,
                                        (last - clutter_from).total_seconds(), rng)
    return detected, len(contacts) - sum(c is not None for c in contacts), last


def run(poll_seconds, dropout_prob, noise_std_m, latency_min, latency_max,
        seed, since_id, height_noise_std_m, model="simple", region=None):
    rng = random.Random(seed)
    cursor = since_id if since_id is not None else db.latest_id("adsb")
    site = radar_model.SITES[region] if region else None
    clutter_from = None
    if model == "realistic":
        print(f"Deriving realistic radar contacts from adsb ticks after id={cursor}, "
              f"every {poll_seconds}s ({site.name if site else 'site chosen from the data'}, "
              f"seed={seed}). Ctrl+C to stop.\n")
    else:
        print(
            f"Deriving synthetic radar contacts from adsb ticks after id={cursor}, "
            f"every {poll_seconds}s (dropout={dropout_prob:.0%}, "
            f"noise_std={noise_std_m}m, latency=[{latency_min},{latency_max}]s, "
            f"seed={seed}). Ctrl+C to stop.\n"
        )
    while True:
        try:
            new_rows = db.ticks_since("adsb", cursor)
            radar_rows = []
            dropped = 0
            if model == "realistic":
                if new_rows:
                    cursor = new_rows[-1]["id"]
                    site = site or radar_model.site_for(new_rows)
                    radar_rows, dropped, clutter_from = realistic_contacts(
                        new_rows, site, rng, clutter_from)
            else:
                for row in new_rows:
                    cursor = row["id"]
                    if rng.random() < dropout_prob:
                        dropped += 1
                        continue
                    radar_rows.append(
                        make_radar_row(row, noise_std_m, latency_min, latency_max, rng,
                                       height_noise_std_m)
                    )
            if new_rows:
                if radar_rows:
                    db.insert_ticks(radar_rows)
                print(
                    f"processed {len(new_rows)} adsb ticks -> "
                    f"{len(radar_rows)} radar contacts, {dropped} not detected"
                )
        except Exception as e:
            print(f"radar_sim tick failed: {e}", file=sys.stderr)

        try:
            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("\nstopped.")
            break


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--poll-seconds", type=float, default=5.0)
    p.add_argument("--dropout-prob", type=float, default=0.1,
                    help="probability a given adsb tick is not detected at all")
    p.add_argument("--noise-std-m", type=float, default=150.0,
                    help="Gaussian position noise std dev, meters")
    p.add_argument("--height-noise-std-m", type=float, default=300.0,
                    help="Gaussian height error std dev, meters (3D radar)")
    p.add_argument("--latency-min", type=float, default=1.0,
                    help="minimum simulated sensor latency, seconds")
    p.add_argument("--latency-max", type=float, default=4.0,
                    help="maximum simulated sensor latency, seconds")
    p.add_argument("--seed", type=int, default=None,
                    help="RNG seed for deterministic replay runs")
    p.add_argument("--model", choices=["simple", "realistic"], default="simple",
                    help="simple: fixed noise and dropout (the original). realistic: "
                         "radar_model.py's site, range-dependent accuracy, horizon, clutter")
    p.add_argument("--region", choices=sorted(radar_model.SITES), default=None,
                    help="realistic: which region's radar site (default: from the data)")
    p.add_argument("--since-id", type=int, default=None,
                    help="resume from this adsb raw_ticks.id instead of "
                         "the current max (useful for backfilling)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(
        poll_seconds=args.poll_seconds,
        dropout_prob=args.dropout_prob,
        noise_std_m=args.noise_std_m,
        latency_min=args.latency_min,
        latency_max=args.latency_max,
        seed=args.seed,
        since_id=args.since_id,
        height_noise_std_m=args.height_noise_std_m,
        model=args.model,
        region=args.region,
    )
