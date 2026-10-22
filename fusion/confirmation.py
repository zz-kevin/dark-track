#!/usr/bin/env python3
"""
confirmation.py — Dark Track / fusion: what track confirmation
(track.CONFIRM_HITS) keeps off the picture, and what it delays.

A track born from a radar contact is tentative until it has
CONFIRM_HITS radar hits. Two numbers say whether the rule is right:

  - false tracks: radar-only tracks that confirmed although most of their
    contacts were clutter, per hour -- what an operator would see that
    isn't there. Next to it, how many tentative tracks were created and
    never confirmed (what the rule kept off the map).
  - never-identified aircraft: real aircraft with their transponder off
    the whole time (anomaly/inject.inject_never_identified removes all
    their ADS-B, one aircraft per run by default -- in a batch, neighbours
    take each other's contacts). Did a confirmed track form
    from their contacts, and how long after the first contact?

Scored against ground truth from ingestion/radar_truth.py.

    python3 confirmation.py <start-iso> <end-iso> [--region gulf] [--radar model]
"""

import argparse
import importlib.util
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import simulate
import track as track_module

ROOT = Path(__file__).resolve().parent.parent
MIN_AIRBORNE_ALT_M = 100.0
# Never-identified candidates need this many radar contacts in the window,
# whatever CONFIRM_HITS is -- so different thresholds score the same set
# (a 4-hit rule failing a 3-contact aircraft is a real cost).
MIN_CONTACTS = 3


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ingestion_db = _load_module("ingestion_db", ROOT / "ingestion" / "db.py")
radar_truth = _load_module("radar_truth", ROOT / "ingestion" / "radar_truth.py")
radar_model = _load_module("radar_model", ROOT / "ingestion" / "radar_model.py")
regions = _load_module("regions", ROOT / "ingestion" / "regions.py")
inject = _load_module("inject", ROOT / "anomaly" / "inject.py")


def _radar_only_tracks(ticks, params, owner):
    """Runs fusion; returns {track_id: {"contacts": [owner...], "first": t,
    "confirmed_at": t|None}} for every track that never had ADS-B.
    owner(raw_id) -> aircraft external_id, "clutter", or None."""
    out = {}
    for cycle in simulate.run(ticks, params):
        for e in cycle["events"]:
            t = cycle["tracks"].get(e["track_id"])
            if t is None or t.adsb_hits:
                out.pop(e["track_id"], None)
                continue
            rec = out.setdefault(e["track_id"], {"contacts": [], "first": e["observed_at"]})
            rec["contacts"].append(owner(e["raw_tick_id"]))
            rec["confirmed_at"] = t.confirmed_at
    return out


def _majority(contacts):
    return Counter(contacts).most_common(1)[0][0]


def evaluate(ticks, params=None, batch=1):
    params = params or simulate.scaled_params(ticks)
    labels, _ = radar_truth.label(ticks)
    clutter_ids = {t["id"] for t in ticks if t.get("truth_tick_id") == radar_truth.CLUTTER}

    def owner(raw_id):
        if raw_id in clutter_ids:
            return "clutter"
        src = labels.get(raw_id)
        return src["external_id"] if src else None

    times = [t["observed_at"] for t in ticks]
    hours = (max(times) - min(times)).total_seconds() / 3600

    tracks = _radar_only_tracks(ticks, params, owner)
    created = len(tracks)
    confirmed = [r for r in tracks.values() if r["confirmed_at"] is not None]
    false_tracks = [r for r in confirmed if _majority(r["contacts"]) == "clutter"]
    clutter_tracks = [r for r in tracks.values() if _majority(r["contacts"]) == "clutter"]

    # Never-identified: airborne aircraft with labelled radar contacts.
    contacts = defaultdict(list)
    for rid, src in labels.items():
        if src["external_id"] and (src["altitude_m"] or 0) >= MIN_AIRBORNE_ALT_M:
            contacts[src["external_id"]].append(rid)
    by_id = {t["id"]: t for t in ticks}
    first_contact = {a: min(by_id[r]["observed_at"] for r in rids) for a, rids in contacts.items()}
    candidates = sorted(a for a, rids in contacts.items() if len(rids) >= MIN_CONTACTS)
    results = {}
    for k in range(0, len(candidates), batch):
        group = candidates[k:k + batch]
        modified = inject.inject_never_identified(ticks, group)
        found = _radar_only_tracks(modified, params, owner)
        for a in group:
            mine = [r for r in found.values()
                    if r["confirmed_at"] is not None and _majority(r["contacts"]) == a]
            results[a] = (min((r["confirmed_at"] - first_contact[a]).total_seconds() for r in mine)
                          if mine else None)

    return {
        "hours": hours, "created": created, "confirmed": len(confirmed),
        "clutter_tracks": len(clutter_tracks), "false_tracks": len(false_tracks),
        "never_identified": results, "params": params,
    }


def report(r):
    print(f"CONFIRM_HITS={track_module.CONFIRM_HITS}; window {r['hours'] * 60:.0f} min, "
          f"adsb interval {r['params']['adsb_interval_s']:.0f}s")
    print(f"radar-only tracks: {r['created']} created, {r['confirmed']} confirmed; "
          f"{r['clutter_tracks']} mostly clutter, of which {r['false_tracks']} confirmed "
          f"= {r['false_tracks'] / r['hours']:.1f} false tracks per hour")
    res = r["never_identified"]
    hits = sorted(d for d in res.values() if d is not None)
    n = len(res)
    print(f"never-identified aircraft confirmed: {len(hits)}/{n} = "
          f"{len(hits) / n:.1%}" if n else "never-identified: no candidates")
    if hits:
        print(f"  time from first contact to confirmed: median {hits[len(hits) // 2]:.0f}s, "
              f"max {hits[-1]:.0f}s")
    missed = sorted(a for a, d in res.items() if d is None)
    if missed:
        print("  not confirmed: " + ", ".join(missed[:10]) + (" ..." if len(missed) > 10 else ""))
    return r


def parse_ts(s):
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("start", type=parse_ts)
    p.add_argument("end", type=parse_ts)
    p.add_argument("--region", choices=sorted(regions.REGIONS), default=None)
    p.add_argument("--radar", choices=["stored", "model"], default="model",
                   help="model (default): the realistic radar, which has clutter")
    p.add_argument("--radar-seed", type=int, default=0)
    p.add_argument("--batch", type=int, default=1,
                   help="aircraft made never-identified per run")
    p.add_argument("--confirm-hits", type=int, default=None,
                   help="override track.CONFIRM_HITS for this run")
    args = p.parse_args()
    if args.confirm_hits is not None:
        track_module.CONFIRM_HITS = args.confirm_hits
    try:
        ticks = regions.pick(ingestion_db.ticks_in_window(args.start, args.end), args.region)
    except ValueError as e:
        raise SystemExit(str(e))
    if not ticks:
        raise SystemExit("no ticks in that window")
    if args.radar == "model":
        ticks = radar_model.regenerate(ticks, args.region, args.radar_seed)
    report(evaluate(ticks, batch=args.batch))


if __name__ == "__main__":
    main()
