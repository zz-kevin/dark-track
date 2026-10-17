#!/usr/bin/env python3
"""
accuracy.py — Dark Track / fusion: association accuracy against ground
truth, scored on docs/PROBLEM.md's fusion target:

    correctly associate the same real-world aircraft across both sources
    within 300 m and 5 s, for 90% of tracks in a test window.

Runs the real fusion engine in memory (simulate.py) over a captured
window and, for every radar contact whose source aircraft is known
(ingestion/radar_truth.py), checks three things:

  - right track: the contact updated the track carrying its own
    aircraft's callsign -- not a neighbour's, not a new anonymous track;
  - within 300 m: the fused position right after that update is within
    300 m of where the aircraft really was at the contact's time;
  - within 5 s: the contact was fused within 5 s of being observed
    (bounded by the poll cadence here; bench.py measures real
    wall-clock latency).

A contact passing all three is correctly associated. An aircraft counts
as correctly associated if at least 90% of its radar contacts are --
the headline number is the share of aircraft that pass. Aircraft whose
ADS-B carries no callsign are excluded (there's no track identity to
check against), as are contacts radar_truth couldn't label.

    python3 accuracy.py <start-iso> <end-iso> [--region nyc|gulf]
"""

import argparse
import importlib.util
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import geo
import simulate

INGESTION_DIR = Path(__file__).resolve().parent.parent / "ingestion"
MAX_ERROR_M = 300.0
MAX_LAG_S = 5.0
PER_AIRCRAFT_PASS = 0.90


def _load_module(name, path):
    # ingestion/ has its own db.py, which `import db` here would shadow.
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ingestion_db = _load_module("ingestion_db", INGESTION_DIR / "db.py")
radar_truth = _load_module("radar_truth", INGESTION_DIR / "radar_truth.py")
regions = _load_module("regions", INGESTION_DIR / "regions.py")


def evaluate(ticks, params=None):
    labels, label_stats = radar_truth.label(ticks)
    params = params or simulate.scaled_params(ticks)
    by_raw_id = {t["id"]: t for t in ticks}

    contacts = []  # one dict per labeled radar contact
    for cycle in simulate.run(ticks, params):
        tracks = cycle["tracks"]
        for e in cycle["events"]:
            if e["source"] != "radar" or e["raw_tick_id"] not in labels:
                continue
            src = labels[e["raw_tick_id"]]
            if src["external_id"] is None:
                continue
            radar = by_raw_id[e["raw_tick_id"]]
            track = tracks.get(e["track_id"])
            true_xy = geo.to_local(*radar_truth.true_position(src, radar["observed_at"]))
            error = math.hypot(e["fused_xy"][0] - true_xy[0], e["fused_xy"][1] - true_xy[1])
            lag = (cycle["poll_time"] - radar["observed_at"]).total_seconds()
            right_track = track is not None and track.external_id == src["external_id"]
            contacts.append({
                "aircraft": src["external_id"],
                "right_track": right_track,
                "new_track": e["created_new_track"],
                "error_m": error,
                "lag_s": lag,
                "airborne": src["altitude_m"] is not None and src["altitude_m"] >= 100.0,
                "correct": right_track and error <= MAX_ERROR_M and lag <= MAX_LAG_S,
            })
    return contacts, label_stats, params


def summarize(contacts, airborne_only=False):
    rows = [c for c in contacts if c["airborne"] or not airborne_only]
    per_aircraft = defaultdict(list)
    for c in rows:
        per_aircraft[c["aircraft"]].append(c["correct"])
    rates = {a: sum(v) / len(v) for a, v in per_aircraft.items()}
    passing = sum(1 for r in rates.values() if r >= PER_AIRCRAFT_PASS)
    right = [c for c in rows if c["right_track"]]
    errors = sorted(c["error_m"] for c in right)
    return {
        "contacts": len(rows),
        "aircraft": len(rates),
        "aircraft_pass": passing,
        "aircraft_rate": passing / len(rates) if rates else float("nan"),
        "contact_correct": sum(c["correct"] for c in rows) / len(rows) if rows else float("nan"),
        "right_track": len(right) / len(rows) if rows else float("nan"),
        "new_track": sum(c["new_track"] for c in rows) / len(rows) if rows else float("nan"),
        "error_p50": errors[len(errors) // 2] if errors else float("nan"),
        "error_p95": errors[int(len(errors) * 0.95)] if errors else float("nan"),
        "lag_max": max((c["lag_s"] for c in rows), default=float("nan")),
        "rates": rates,
    }


def report(contacts, label_stats, params):
    total = sum(label_stats.values())
    print(f"window adsb interval {params['adsb_interval_s']:.0f}s; radar contacts {total}: "
          f"{label_stats['exact']} exact truth, {label_stats['inferred']} inferred, "
          f"{label_stats['ambiguous']} ambiguous, {label_stats['unmatched']} unmatched "
          f"(last two unscored)")
    for name, airborne_only in (("all traffic", False), ("airborne only", True)):
        s = summarize(contacts, airborne_only)
        print(f"\n{name}: {s['contacts']} contacts from {s['aircraft']} aircraft")
        print(f"  aircraft correctly associated (>= {PER_AIRCRAFT_PASS:.0%} of contacts): "
              f"{s['aircraft_pass']}/{s['aircraft']} = {s['aircraft_rate']:.1%}  "
              f"(target 90%)")
        print(f"  contacts correct: {s['contact_correct']:.1%}  "
              f"(on own track {s['right_track']:.1%}, spawned a new track {s['new_track']:.1%})")
        print(f"  fused position error on own track: p50 {s['error_p50']:.0f} m, "
              f"p95 {s['error_p95']:.0f} m; max fuse lag {s['lag_max']:.1f} s")
        if not airborne_only:
            worst = sorted(s["rates"].items(), key=lambda kv: kv[1])[:5]
            print("  worst aircraft: " + ", ".join(f"{a} {r:.0%}" for a, r in worst))
    return summarize(contacts)


def parse_ts(s):
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("start", type=parse_ts)
    p.add_argument("end", type=parse_ts)
    p.add_argument("--region", choices=sorted(regions.REGIONS), default=None,
                   help="required when the window holds more than one region's traffic")
    args = p.parse_args()
    try:
        ticks = regions.pick(ingestion_db.ticks_in_window(args.start, args.end), args.region)
    except ValueError as e:
        raise SystemExit(str(e))
    if not ticks:
        print("no ticks in that window")
        return
    report(*evaluate(ticks))


if __name__ == "__main__":
    main()
