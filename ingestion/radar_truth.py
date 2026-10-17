"""
radar_truth.py — Dark Track / ingestion: ground truth for synthetic radar
contacts. Evaluation only -- fusion/ never sees any of this.

A radar contact deliberately carries no ID, so nothing downstream can
tell which aircraft it came from. Scoring fusion (fusion/accuracy.py) and
injecting maneuvers that move both sensors (anomaly/inject.py) both need
exactly that link, so this module recovers it:

  - Contacts written since radar_sim.py started recording it carry
    `truth_tick_id`, the ADS-B row they were derived from. Exact.
  - Older contacts are matched back by inverting radar_sim.make_radar_row:
    the source ADS-B tick is ~latency_min..latency_max seconds earlier,
    has the identical altitude_m (copied through unchanged), speed and
    heading within radar_sim's small jitter of the contact's, and
    dead-reckoned forward by the latency lands within a few noise std
    devs of the contact. A contact with two plausible sources (or none)
    is left unlabeled rather than guessed, and counted.
"""

import bisect
import math
from datetime import timedelta

METERS_PER_DEG_LAT = 111320.0


def advance(lat, lon, velocity, heading, seconds):
    """Dead-reckon a position forward along the reported track (same as
    radar_sim.advance)."""
    if velocity is None or heading is None:
        return lat, lon
    dist = velocity * seconds
    dn = dist * math.cos(math.radians(heading))
    de = dist * math.sin(math.radians(heading))
    return (lat + dn / METERS_PER_DEG_LAT,
            lon + de / (METERS_PER_DEG_LAT * math.cos(math.radians(lat)) or 1e-9))


def distance_m(lat1, lon1, lat2, lon2):
    dn = (lat2 - lat1) * METERS_PER_DEG_LAT
    de = (lon2 - lon1) * METERS_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dn, de)


def true_position(source_tick, at):
    """Where the aircraft really was at `at`, noise-free: the source ADS-B
    fix dead-reckoned forward -- exactly the point radar_sim jittered."""
    dt = (at - source_tick["observed_at"]).total_seconds()
    return advance(source_tick["lat"], source_tick["lon"],
                   source_tick["velocity_mps"], source_tick["heading_deg"], dt)


def _kinematics_match(src, r, sigmas=4.0):
    """radar_sim reports speed + N(0, 2 m/s) and heading + N(0, 3 deg)."""
    if (src["velocity_mps"] is None) != (r["velocity_mps"] is None):
        return False
    if src["velocity_mps"] is not None and abs(src["velocity_mps"] - r["velocity_mps"]) > sigmas * 2.0:
        return False
    if (src["heading_deg"] is None) != (r["heading_deg"] is None):
        return False
    if src["heading_deg"] is not None:
        diff = abs((src["heading_deg"] - r["heading_deg"] + 180.0) % 360.0 - 180.0)
        if diff > sigmas * 3.0:
            return False
    return True


def label(ticks, latency_min=1.0, latency_max=4.0, noise_std_m=150.0):
    """Returns ({radar_id: source adsb tick}, stats). Defaults match
    radar_sim.py's own defaults."""
    adsb = sorted((t for t in ticks if t["source"] == "adsb"), key=lambda t: t["observed_at"])
    by_id = {t["id"]: t for t in adsb}
    times = [t["observed_at"] for t in adsb]
    # 2D Gaussian noise: radial error beyond ~4 std devs is a ~1-in-3000 draw.
    max_dist = 4.0 * noise_std_m
    slack = timedelta(seconds=0.05)

    labels = {}
    stats = {"exact": 0, "inferred": 0, "ambiguous": 0, "unmatched": 0}
    for r in ticks:
        if r["source"] != "radar":
            continue
        if r.get("truth_tick_id") is not None:
            src = by_id.get(r["truth_tick_id"])
            if src is not None:
                labels[r["id"]] = src
                stats["exact"] += 1
            else:
                stats["unmatched"] += 1  # source fell outside the window
            continue

        lo = bisect.bisect_left(times, r["observed_at"] - timedelta(seconds=latency_max) - slack)
        hi = bisect.bisect_right(times, r["observed_at"] - timedelta(seconds=latency_min) + slack)
        plausible = []
        for src in adsb[lo:hi]:
            if src["altitude_m"] != r["altitude_m"] or not _kinematics_match(src, r):
                continue
            lat, lon = true_position(src, r["observed_at"])
            d = distance_m(lat, lon, r["lat"], r["lon"])
            if d <= max_dist:
                plausible.append((d, src))
        # Two candidate fixes from the *same* aircraft still identify it.
        aircraft = {src.get("icao24") or src["external_id"] for _, src in plausible}
        if len(aircraft) == 1:
            labels[r["id"]] = min(plausible, key=lambda p: p[0])[1]
            stats["inferred"] += 1
        elif plausible:
            stats["ambiguous"] += 1
        else:
            stats["unmatched"] += 1
    return labels, stats
