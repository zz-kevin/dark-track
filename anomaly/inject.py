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


def _accelerated_velocity(tick, north_dir, east_dir, dv):
    """Reported speed/heading after adding dv m/s along (north_dir,
    east_dir) -- so a maneuver's ADS-B velocity agrees with its positions
    (fusion measures ADS-B velocity, engine.ADSB_VEL_STD_MPS)."""
    v, hdg = tick.get("velocity_mps"), tick.get("heading_deg")
    if v is None or hdg is None:
        return {}
    ve = v * math.sin(math.radians(hdg)) + east_dir * dv
    vn = v * math.cos(math.radians(hdg)) + north_dir * dv
    return {"velocity_mps": math.hypot(ve, vn),
            "heading_deg": math.degrees(math.atan2(ve, vn)) % 360.0}


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


def inject_never_identified(ticks, external_ids):
    """Removes every ADS-B tick of each aircraft in `external_ids`: aircraft
    flying with their transponder off the whole time, seen only by radar.
    Radar contacts are left as they are -- generate them (radar_model
    regenerate) before calling this, from the full ADS-B. Returns the
    modified ticks; which radar contacts belong to whom is the caller's
    (ingestion/radar_truth.label on the unmodified ticks)."""
    gone = set(external_ids)
    return [t for t in ticks if not (t["source"] == "adsb" and t["external_id"] in gone)]


def inject_kinematic_jump(ticks, external_id, cutoff_fraction=0.5,
                           lateral_accel_mps2=20.0, seed=None, radar_sources=None):
    """Simulates a sustained maneuver the constant-velocity model can't
    explain: after a cutoff timestamp, `external_id`'s ADS-B positions are
    displaced by 0.5 * a * t^2 along a random bearing -- a constant
    lateral acceleration of lateral_accel_mps2 (default ~2g, far outside
    what an airliner does and ~7x fusion's accel_std process noise).

    Reported speed/heading change to match (velocity grows by a * t), so
    the maneuver is self-consistent for a filter that measures ADS-B
    velocity. Displacement grows with *time*, not tick count, so the same maneuver
    is injected at any poll cadence. (The previous model added a fixed
    400m per tick: a constant sideways *velocity*, which the
    constant-velocity filter simply learned as a new velocity after a
    couple of updates -- measured: zero alerts on the target.) Sustained
    rather than a one-tick jump, so the residual streak spans the
    consecutive updates kinematic_inconsistency() requires.

    `radar_sources` ({radar tick id: source adsb tick}, from
    ingestion/radar_truth.label) moves the target's radar contacts by the
    same maneuver, evaluated at each contact's own time -- a real maneuver
    moves the aircraft, so both sensors see it. Without it only ADS-B
    moves, the target's track loses its radar, and the orphaned contacts
    start new tracks (the old harness's "induced" false alarms).

    Returns (modified_ticks, ground_truth) where ground_truth is
    {"external_id", "kind": "kinematic_inconsistency", "injected_at"}.

    `seed` fixes the maneuver's direction (harness.py seeds it per
    aircraft, so a sweep is reproducible).

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
    after_ids = {t["id"] for t in after}
    target_ids = {t["id"] for t in adsb_ticks}
    radar_ids = {
        rid for rid, src in (radar_sources or {}).items() if src["id"] in target_ids
    }

    modified = []
    for t in ticks:
        moves = (t["id"] in after_ids if t["source"] == "adsb"
                 else t["id"] in radar_ids and t["observed_at"] > cutoff_at)
        if moves:
            elapsed = (t["observed_at"] - cutoff_at).total_seconds()
            offset_m = 0.5 * lateral_accel_mps2 * elapsed ** 2
            new_lat, new_lon = _offset_latlon(
                t["lat"], t["lon"], north_dir * offset_m, east_dir * offset_m
            )
            t = {**t, "lat": new_lat, "lon": new_lon, **_accelerated_velocity(
                t, north_dir, east_dir, lateral_accel_mps2 * elapsed)}
        modified.append(t)

    ground_truth = {
        "external_id": external_id,
        "kind": "kinematic_inconsistency",
        "injected_at": cutoff_at,
    }
    return modified, ground_truth
