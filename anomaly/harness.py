#!/usr/bin/env python3
"""
harness.py — Dark Track / anomaly: precision/recall test harness.

Runs the *real* fusion pipeline (fusion/engine.py's apply_identified /
apply_anonymous / prune_stale via fusion/simulate.py -- not reimplemented) and
the *real* detectors (detectors.py) against a real, already-captured
replay window with one anomaly synthetically injected (inject.py),
advancing in simulated poll cycles just like the two live engines would --
all in-memory, no live Postgres writes, no real-time sleeping. Scores
against docs/PROBLEM.md's stated success criteria:

  - Went-dark: flagged within adsb_silence_s + one ADS-B update interval
    of the injected event (~41s at 10s polling, ~115s at 45s).
  - False-positive rate: share of aircraft that raise a false alarm on
    the un-injected window (ordinary traffic), each alert counted once;
    alerts the injection itself causes are reported separately.
  - (Kinematic inconsistency has no stated latency target in
    docs/PROBLEM.md; budget is (consecutive + 1) update intervals.)

Usage:

    python3 harness.py sweep went_dark <start-iso> <end-iso>
    python3 harness.py sweep kinematic <start-iso> <end-iso>
    python3 harness.py one went_dark <start-iso> <end-iso> --external-id ABC123
    (add --region nyc|gulf when both pollers ran during the window)

Requires a real captured window already in `raw_ticks` (both sources) --
run ingestion/opensky_poll.py + ingestion/radar_sim.py for a while first.
"""

import argparse
import importlib.util
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FUSION_DIR = ROOT / "fusion"
INGESTION_DIR = ROOT / "ingestion"

# fusion/simulate.py does `import engine`, `import geo`, ... at its own
# top level -- those need fusion/ on sys.path (ahead of anomaly/'s own dir,
# which also has a db.py) at the moment this import statement runs.
sys.path.insert(0, str(FUSION_DIR))
import simulate  # noqa: E402
sys.path.remove(str(FUSION_DIR))


def _load_module(name, path):
    """ingestion/db.py can't be `import db` here -- that name is already
    bound (via fusion/engine.py's own import, above) to fusion/db.py, and
    anomaly/ has a third, different db.py. Load it directly by path
    instead of relying on sys.path + module name resolution."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ingestion_db = _load_module("ingestion_db", INGESTION_DIR / "db.py")
radar_truth = _load_module("radar_truth", INGESTION_DIR / "radar_truth.py")
regions = _load_module("regions", INGESTION_DIR / "regions.py")

import inject  # noqa: E402
from detectors import (  # noqa: E402
    MIN_AIRBORNE_ALT_M, kinematic_inconsistency, went_dark, went_dark_thresholds,
)

DEFAULTS = dict(
    **simulate.DEFAULT_PARAMS,
    # 10s (the naive "poll interval + margin" guess) turned out to be
    # swamped by real OpenSky poll jitter -- see anomaly/README.md's
    # measured numbers and docs/PROBLEM.md's success-criteria note. 30s
    # clears ordinary jitter in the captured data; genuinely long
    # absences (aircraft left the bounding box) still alias with this,
    # which is a separate, irreducible limitation, not a threshold
    # tuning problem.
    adsb_silence_s=30.0,
    radar_recency_s=10.0,
    sigma_mult=4.0,
    consecutive=2,
)


def run_scenario(ticks, params):
    """Runs the real fusion engine (fusion/simulate.py) + both real
    detectors over `ticks` in simulated poll cycles. Returns a list of
    detected anomaly dicts: {kind, track_id, external_id, detected_at}."""
    kinematic_state = {}
    open_went_dark = set()
    detections = []

    for cycle in simulate.run(ticks, params):
        tracks, poll_time = cycle["tracks"], cycle["poll_time"]
        snapshot = [
            {"track_id": t.id, "external_id": t.external_id, "adsb_hits": t.adsb_hits,
             "last_adsb_at": t.last_adsb_at, "last_radar_at": t.last_radar_at,
             "last_adsb_alt_m": t.last_adsb_alt_m}
            for t in tracks.values()
        ]
        dark_hits = went_dark(
            snapshot, poll_time, params["adsb_silence_s"], params["radar_recency_s"]
        )
        currently_open = {h["track_id"] for h in dark_hits}
        for h in dark_hits:
            if h["track_id"] not in open_went_dark:
                detections.append(h)
        open_went_dark = currently_open

        kinem_detected, _kinem_resolved, kinematic_state = kinematic_inconsistency(
            cycle["events"], kinematic_state, params["sigma_mult"], params["consecutive"]
        )
        for a in kinem_detected:
            track = tracks.get(a["track_id"])
            a["external_id"] = track.external_id if track else None
        detections.extend(kinem_detected)

    return detections


def score(detections, ground_truth, latency_budget_s):
    """One ground-truth injected event (the only one in this run) vs. every
    detection the run produced. A detection matches if it's the same kind
    and external_id, and lands within [0, latency_budget_s] of injected_at
    -- anything else (wrong track, wrong kind, or the right track outside
    the budget) counts as a false positive."""
    true_positive = None
    false_positives = []
    for d in detections:
        matches_target = (
            d["kind"] == ground_truth["kind"]
            and d.get("external_id") == ground_truth["external_id"]
        )
        latency = (d["detected_at"] - ground_truth["injected_at"]).total_seconds()
        if matches_target and 0 <= latency <= latency_budget_s and true_positive is None:
            true_positive = {"latency_s": latency}
        else:
            false_positives.append(d)
    return true_positive, false_positives


def eligible_external_ids(ticks, min_adsb_ticks=5):
    counts = defaultdict(int)
    for t in ticks:
        if t["source"] == "adsb" and t["external_id"]:
            counts[t["external_id"]] += 1
    return [eid for eid, n in counts.items() if n >= min_adsb_ticks]


KIND_NAME = {"went_dark": "went_dark", "kinematic": "kinematic_inconsistency"}


def _alert_key(d):
    return d["kind"], d.get("external_id"), d["detected_at"]


def default_latency_budget(kind, params):
    """docs/PROBLEM.md's target, in terms of the window's own cadence.
    Went-dark can't fire before ADS-B has been silent adsb_silence_s, and
    the radar contact that confirms it arrives up to one update interval
    later. Kinematic needs `consecutive` over-threshold updates, plus one
    interval of slack for where the cutoff falls between updates."""
    interval = params["adsb_interval_s"]
    if kind == "went_dark":
        return params["adsb_silence_s"] + interval
    return (params["consecutive"] + 1) * interval


def _airborne_at(ticks, external_id, at):
    before = [t for t in ticks if t["source"] == "adsb" and t["external_id"] == external_id
              and t["observed_at"] <= at]
    if not before:
        return False
    alt = max(before, key=lambda t: t["observed_at"])["altitude_m"]
    return alt is not None and alt >= MIN_AIRBORNE_ALT_M


def sweep(kind, ticks, params, latency_budget_s):
    """Recall: one injected event per eligible aircraft, each its own run.

    False alarms are scored in two parts, so the same alert isn't counted
    once per run (the old n*(n-1) metric re-counted every real-traffic
    alert in every one of n runs):
      - background: alerts of this kind on the *un-injected* window, i.e.
        what the detector does on ordinary traffic. The headline rate is
        the share of aircraft with at least one -- docs/PROBLEM.md's
        "false-positive rate under 5%".
      - induced: alerts in an injected run that aren't the true positive
        and weren't already in the background -- side effects of the
        injection itself (e.g. the target's own radar contacts orphaned
        into a new track), averaged per run.
    Only alerts of the detector being swept count; a went-dark sweep
    doesn't score kinematic alerts as its false positives."""
    kind_name = KIND_NAME[kind]
    candidates = eligible_external_ids(ticks)
    background = [d for d in run_scenario(ticks, params) if d["kind"] == kind_name]
    background_keys = {_alert_key(d) for d in background}
    radar_sources, _ = radar_truth.label(ticks) if kind == "kinematic" else ({}, None)
    results = []
    induced = []
    skipped = []

    for eid in candidates:
        try:
            if kind == "went_dark":
                modified, gt = inject.inject_went_dark(ticks, eid)
            else:
                modified, gt = inject.inject_kinematic_jump(ticks, eid, radar_sources=radar_sources)
        except ValueError as e:
            skipped.append((eid, str(e)))
            continue
        if kind == "went_dark" and not _airborne_at(ticks, eid, gt["injected_at"]):
            # went_dark() deliberately ignores ground traffic, so a dropout
            # injected while taxiing isn't a detectable event to score.
            skipped.append((eid, "on the ground at the injected dropout"))
            continue

        detections = [d for d in run_scenario(modified, params) if d["kind"] == kind_name]
        tp, fps = score(detections, gt, latency_budget_s)
        results.append({"external_id": eid, "true_positive": tp})
        induced.extend(d for d in fps if _alert_key(d) not in background_keys)

    n = len(results)
    hits = [r for r in results if r["true_positive"]]
    recall = len(hits) / n if n else float("nan")
    latencies = [h["true_positive"]["latency_s"] for h in hits]
    noisy_aircraft = {d["external_id"] for d in background if d.get("external_id")}
    anon_alerts = sum(1 for d in background if not d.get("external_id"))
    fpr = len(noisy_aircraft) / len(candidates) if candidates else float("nan")
    times = [t["observed_at"] for t in ticks]
    hours = (max(times) - min(times)).total_seconds() / 3600

    print(f"kind={kind} window candidates={len(candidates)} scored={n} skipped={len(skipped)} "
          f"latency budget={latency_budget_s:.0f}s")
    for eid, reason in skipped:
        print(f"  skipped {eid}: {reason}")
    print(f"recall: {len(hits)}/{n} = {recall:.1%}" if n else "recall: n/a (no eligible tracks)")
    if latencies:
        print(f"detection latency: mean={sum(latencies)/len(latencies):.1f}s max={max(latencies):.1f}s")
    print(f"false-positive rate: {len(noisy_aircraft)}/{len(candidates)} aircraft = {fpr:.1%} "
          f"raised a false alarm on ordinary traffic ({len(background)} alerts, "
          f"{anon_alerts} on unidentified tracks, "
          f"{len(background) / (len(candidates) * hours) if hours else 0:.2f} per aircraft-hour)")
    print(f"injection-induced false alarms: {len(induced)} over {n} runs "
          f"= {len(induced) / n if n else 0:.2f} per run")
    return {"recall": recall, "latencies": latencies, "fpr": fpr, "n": n,
            "background_alerts": len(background), "induced": len(induced)}


def parse_ts(s):
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="mode", required=True)

    for mode in ("sweep", "one"):
        sp = sub.add_parser(mode)
        sp.add_argument("kind", choices=["went_dark", "kinematic"])
        sp.add_argument("start", type=parse_ts)
        sp.add_argument("end", type=parse_ts)
        if mode == "one":
            sp.add_argument("--external-id", required=True)
        sp.add_argument("--region", choices=sorted(regions.REGIONS), default=None,
                        help="required when the window holds more than one region's traffic")
        sp.add_argument("--latency-budget-s", type=float, default=None,
                        help="default: docs/PROBLEM.md's target scaled to the "
                             "window's cadence (see default_latency_budget)")
    return p.parse_args()


def scaled_params(ticks):
    fusion = simulate.scaled_params(ticks)
    interval = fusion["adsb_interval_s"]
    silence, recency = went_dark_thresholds(interval)
    print(f"window adsb interval {interval:.0f}s -> gate {fusion['gate_staleness_s']:.0f}s, "
          f"ttl {fusion['track_ttl_s']:.0f}s, silence {silence:.0f}s, "
          f"radar recency {recency:.0f}s")
    return {**DEFAULTS, **fusion, "adsb_silence_s": silence, "radar_recency_s": recency}


def main():
    args = parse_args()
    try:
        ticks = regions.pick(ingestion_db.ticks_in_window(args.start, args.end), args.region)
    except ValueError as e:
        sys.exit(str(e))
    if not ticks:
        print("no ticks in that window")
        return
    params = scaled_params(ticks)
    budget = (args.latency_budget_s if args.latency_budget_s is not None
              else default_latency_budget(args.kind, params))
    kind = "went_dark" if args.kind == "went_dark" else "kinematic_inconsistency"

    if args.mode == "sweep":
        sweep(args.kind, ticks, params, budget)
    else:
        if args.kind == "went_dark":
            modified, gt = inject.inject_went_dark(ticks, args.external_id)
        else:
            radar_sources, _ = radar_truth.label(ticks)
            modified, gt = inject.inject_kinematic_jump(ticks, args.external_id,
                                                        radar_sources=radar_sources)
        detections = run_scenario(modified, params)
        tp, fps = score(detections, gt, budget)
        print(f"ground truth: {gt}")
        print(f"true positive: {tp}")
        print(f"false positives ({len(fps)}): {fps}")


if __name__ == "__main__":
    main()
