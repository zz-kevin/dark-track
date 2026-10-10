"""
inject.py — Dark Track / anomaly: synthetic anomaly injection for
harness.py's precision/recall scoring.

Takes a real, already-captured window of raw_ticks (both real ADS-B and
the radar contacts already independently derived from it by
ingestion/radar_sim.py) and returns a *modified* copy plus the ground
truth of what was changed and when -- so harness.py can run the real
fusion + anomaly pipeline against it and check whether the detectors
noticed.

Deliberately not a synthetic flight generator: modifying a real capture
means the untouched parts of the scenario (busy-airspace association
ambiguity, real sensor noise, real radar dropout) are the same ones
fusion/ and anomaly/ have to handle live, not an idealized stand-in for
them.
"""

import math
import random

METERS_PER_DEG_LAT = 111320.0


def _offset_latlon(lat, lon, north_m, east_m):
    dlat = north_m / METERS_PER_DEG_LAT
    dlon = east_m / (METERS_PER_DEG_LAT * math.cos(math.radians(lat)) or 1e-9)
    return lat + dlat, lon + dlon


def inject_went_dark(ticks, external_id, cutoff_fraction=0.5):
    """Drops `external_id`'s ADS-B ticks after a cutoff timestamp chosen at
    cutoff_fraction through that aircraft's span in the window. Radar
    ticks are left untouched -- they were already generated independently
    from the full, un-truncated ADS-B feed by radar_sim.py, so "radar
    keeps flying the same profile" falls out for free rather than needing
    to be simulated.

    Returns (modified_ticks, ground_truth) where ground_truth is
    {"external_id", "kind": "went_dark", "injected_at"}.

    Raises ValueError if `external_id` has no ADS-B ticks in the window, or
    no radar ticks after the chosen cutoff (i.e. this window can't actually
    exercise the "radar still sees it" half of the scenario).
    """
    adsb_ticks = [t for t in ticks if t["source"] == "adsb" and t["external_id"] == external_id]
    if not adsb_ticks:
        raise ValueError(f"no adsb ticks for {external_id!r} in this window")

    times = sorted(t["observed_at"] for t in adsb_ticks)
    cutoff_at = times[0] + (times[-1] - times[0]) * cutoff_fraction

    radar_after = [t for t in ticks if t["source"] == "radar" and t["observed_at"] > cutoff_at]
    if not radar_after:
        raise ValueError(
            f"no radar ticks after cutoff for {external_id!r} -- can't exercise "
            f"went-dark in this window (radar dropout, or window too short)"
        )

    modified = [
        t for t in ticks
        if not (t["source"] == "adsb" and t["external_id"] == external_id
                and t["observed_at"] > cutoff_at)
    ]
    ground_truth = {"external_id": external_id, "kind": "went_dark", "injected_at": cutoff_at}
    return modified, ground_truth


def inject_kinematic_jump(ticks, external_id, cutoff_fraction=0.5,
                           ramp_m_per_tick=400.0, seed=None):
    """Simulates a sudden, sustained maneuver the constant-velocity model
    can't explain: after a cutoff timestamp, each of `external_id`'s ADS-B
    ticks gets pushed further sideways (perpendicular to its original
    track, a hard turn) than the last, by ramp_m_per_tick more each time --
    a sustained maneuver rather than one clean position jump, so the
    resulting residual streak is sustained across consecutive updates
    (what kinematic_inconsistency()'s `consecutive` check requires) rather
    than a single one-tick outlier that would just look like sensor noise.

    Returns (modified_ticks, ground_truth) where ground_truth is
    {"external_id", "kind": "kinematic_inconsistency", "injected_at"}.

    Raises ValueError if `external_id` has fewer than 3 ADS-B ticks after
    the chosen cutoff (too few to form a "consecutive" streak at all).
    """
    rng = random.Random(seed)
    adsb_ticks = sorted(
        (t for t in ticks if t["source"] == "adsb" and t["external_id"] == external_id),
        key=lambda t: t["observed_at"],
    )
    if not adsb_ticks:
        raise ValueError(f"no adsb ticks for {external_id!r} in this window")

    cutoff_at = adsb_ticks[0]["observed_at"] + (
        adsb_ticks[-1]["observed_at"] - adsb_ticks[0]["observed_at"]
    ) * cutoff_fraction
    after = [t for t in adsb_ticks if t["observed_at"] > cutoff_at]
    if len(after) < 3:
        raise ValueError(
            f"only {len(after)} adsb ticks after cutoff for {external_id!r} -- "
            f"need at least 3 to form a sustained maneuver"
        )

    bearing = rng.uniform(0, 2 * math.pi)
    north_dir, east_dir = math.cos(bearing), math.sin(bearing)
    rank_by_id = {t["id"]: i + 1 for i, t in enumerate(after)}  # 1-indexed

    modified = []
    for t in ticks:
        rank = rank_by_id.get(t["id"])
        if rank is not None:
            offset_m = ramp_m_per_tick * rank
            new_lat, new_lon = _offset_latlon(
                t["lat"], t["lon"], north_dir * offset_m, east_dir * offset_m
            )
            t = {**t, "lat": new_lat, "lon": new_lon}
        modified.append(t)

    ground_truth = {
        "external_id": external_id,
        "kind": "kinematic_inconsistency",
        "injected_at": cutoff_at,
    }
    return modified, ground_truth
