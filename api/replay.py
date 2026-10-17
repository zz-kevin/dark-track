"""
replay.py — Dark Track / api: replay a stored window as map frames.

Runs the real fusion engine (fusion/simulate.py) and both real detectors
(anomaly/detectors.py) over a captured window of raw_ticks, in memory,
and samples the fused picture every `step_s` seconds for web/'s replay
player. Optionally injects one anomaly first (anomaly/inject.py, the same
injection the precision/recall harness scores), so a demo can show a
known went-dark or maneuver on real traffic.

Nothing is written to the database.
"""

import importlib.util
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_module(name, path):
    # ingestion/, fusion/, anomaly/ and api/ each have their own db.py.
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# fusion/simulate.py imports its siblings by bare name (engine, db, geo,
# ...), and `db` would otherwise resolve to whichever db.py was imported
# first -- so import fusion with its own dir first on the path and the
# name `db` unclaimed, then put back whatever held it.
_held_db = sys.modules.pop("db", None)
sys.path.insert(0, str(ROOT / "fusion"))
import geo  # noqa: E402
import simulate  # noqa: E402
sys.path.remove(str(ROOT / "fusion"))
sys.modules.pop("db", None)
if _held_db is not None:
    sys.modules["db"] = _held_db

ingestion_db = _load_module("ingestion_db", ROOT / "ingestion" / "db.py")
radar_truth = _load_module("radar_truth", ROOT / "ingestion" / "radar_truth.py")
regions = _load_module("regions", ROOT / "ingestion" / "regions.py")
aircraft_types = _load_module("aircraft_types", ROOT / "ingestion" / "aircraft_types.py")
detectors = _load_module("detectors", ROOT / "anomaly" / "detectors.py")
inject = _load_module("inject", ROOT / "anomaly" / "inject.py")

MAX_WINDOW_S = 3600


def candidates(ticks, min_adsb_ticks=5):
    """Callsigns with enough airborne ADS-B in the window to inject on."""
    counts = defaultdict(int)
    airborne = set()
    for t in ticks:
        if t["source"] == "adsb" and t["external_id"]:
            counts[t["external_id"]] += 1
            if (t["altitude_m"] or 0) >= detectors.MIN_AIRBORNE_ALT_M:
                airborne.add(t["external_id"])
    return sorted(eid for eid, n in counts.items() if n >= min_adsb_ticks and eid in airborne)


def _state(track, now, silence_s, dark_ids):
    if track.id in dark_ids:
        return "dark"
    if track.last_adsb_at and (now - track.last_adsb_at).total_seconds() <= silence_s:
        return "adsb"
    if track.last_adsb_at is None:
        return "radar"
    return "stale"


def window_ticks(start, end, region):
    return regions.pick(ingestion_db.ticks_in_window(start, end), region)


def run(start, end, step_s=6.0, inject_kind=None, target=None, region=None):
    ticks = window_ticks(start, end, region)
    if not ticks:
        return {"error": "no ticks in that window"}

    injected = None
    if inject_kind and target:
        if inject_kind == "went_dark":
            ticks, injected = inject.inject_went_dark(ticks, target)
        elif inject_kind == "kinematic":
            sources, _ = radar_truth.label(ticks)
            ticks, injected = inject.inject_kinematic_jump(
                ticks, target, radar_sources=sources, seed=0)
        else:
            raise ValueError(f"unknown injection {inject_kind!r}")

    params = simulate.scaled_params(ticks)
    silence, recency = detectors.went_dark_thresholds(params["adsb_interval_s"])

    meta, frames, alerts = {}, [], []
    kinematic_state, open_dark = {}, {}
    next_frame = None
    for cycle in simulate.run(ticks, params):
        tracks, now = cycle["tracks"], cycle["poll_time"]

        snapshot = [
            {"track_id": t.id, "external_id": t.external_id, "adsb_hits": t.adsb_hits,
             "last_adsb_at": t.last_adsb_at, "last_radar_at": t.last_radar_at,
             "last_adsb_alt_m": t.last_adsb_alt_m}
            for t in tracks.values()
        ]
        new_dark, _, open_dark = detectors.went_dark_episodes(
            snapshot, now, open_dark, silence, recency)
        for h in new_dark:
            alerts.append({"t": now.timestamp(), "track_id": h["track_id"],
                           "external_id": h["external_id"], "kind": "went_dark",
                           "metric": round(h["metric"], 1)})
        detected, _, kinematic_state = detectors.kinematic_inconsistency(
            cycle["events"], kinematic_state)
        for a in detected:
            track = tracks.get(a["track_id"])
            alerts.append({"t": a["detected_at"].timestamp(), "track_id": a["track_id"],
                           "external_id": track.external_id if track else None,
                           "kind": "kinematic_inconsistency",
                           "metric": round(a["metric"], 1)})

        if next_frame is None or now >= next_frame:
            rows = []
            for t in tracks.values():
                if t.id not in meta:
                    meta[t.id] = {"external_id": t.external_id}
                if t.aircraft_type and "type" not in meta[t.id]:
                    meta[t.id]["type"] = aircraft_types.label(t.aircraft_type, t.aircraft_variant)
                lat, lon = geo.to_latlon(*t.kf.position)
                heading = t.heading_deg()
                rows.append([t.id, round(lat, 5), round(lon, 5),
                             None if heading is None else round(heading),
                             round(t.speed_mps()), _state(t, now, silence, open_dark),
                             t.confidence(now)])
            frames.append({"t": now.timestamp(), "tracks": rows})
            next_frame = now + timedelta(seconds=step_s)

    return {
        "start": start.isoformat(), "end": end.isoformat(), "step_s": step_s, "region": region,
        "adsb_interval_s": params["adsb_interval_s"],
        "thresholds": {"silence_s": silence, "radar_recency_s": recency},
        "injected": injected and {**injected, "injected_at": injected["injected_at"].timestamp()},
        "tracks": meta, "frames": frames, "alerts": alerts,
    }
