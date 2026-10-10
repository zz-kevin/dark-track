#!/usr/bin/env python3
"""
harness.py — Dark Track / anomaly: precision/recall test harness.

Runs the *real* fusion pipeline (fusion/engine.py's apply_identified /
apply_anonymous / prune_stale, imported directly -- not reimplemented) and
the *real* detectors (detectors.py) against a real, already-captured
replay window with one anomaly synthetically injected (inject.py),
advancing in simulated poll cycles just like the two live engines would --
all in-memory, no live Postgres writes, no real-time sleeping. Scores
against docs/PROBLEM.md's stated success criteria:

  - Went-dark: flagged within 15s of the injected event.
  - False-positive rate: any alert on a *non*-injected track, across the
    whole sweep, counts against it.
  - (Kinematic inconsistency has no stated latency target in
    docs/PROBLEM.md; scored the same way for comparability.)

Usage:

    python3 harness.py sweep went_dark <start-iso> <end-iso>
    python3 harness.py sweep kinematic <start-iso> <end-iso>
    python3 harness.py one went_dark <start-iso> <end-iso> --external-id ABC123

Requires a real captured window already in `raw_ticks` (both sources) --
run ingestion/opensky_poll.py + ingestion/radar_sim.py for a while first.
"""

import argparse
import importlib.util
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FUSION_DIR = ROOT / "fusion"
INGESTION_DIR = ROOT / "ingestion"

# fusion/engine.py does `import db`, `import geo`, `from associate import
# associate`, `from track import Track` at its own top level -- those need
# fusion/ on sys.path (ahead of anomaly/'s own dir, which also has a
# db.py) at the moment this import statement runs.
sys.path.insert(0, str(FUSION_DIR))
import engine as fusion_engine  # noqa: E402
import geo as fusion_geo  # noqa: E402
sys.path.remove(str(FUSION_DIR))

# fusion_engine.prune_stale() calls db.delete_fused_track() as a side
# effect -- fine for the live engine, but this harness runs entirely
# in-memory and must never touch the real DATABASE_URL. Silence just that
# one call; prune_stale's actual pruning logic (which the harness needs)
# is untouched.
fusion_engine.db.delete_fused_track = lambda track_id: None


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

import inject  # noqa: E402
from detectors import kinematic_inconsistency, went_dark, went_dark_thresholds  # noqa: E402

DEFAULTS = dict(
    poll_seconds=3.0,
    accel_std=3.0,
    gate_dist_m=800.0,
    gate_staleness_s=30.0,
    track_ttl_s=120.0,
    r_adsb_std_m=20.0,
    r_radar_std_m=150.0,
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


def _poll_cycles(ticks, poll_seconds):
    """Buckets `ticks` into fixed poll_seconds-wide windows spanning the
    data, same mental model as fusion/engine.py's live poll loop: at each
    poll, whatever arrived since the last poll gets processed as one
    batch. Yields (poll_time, batch) in order, including empty batches
    (a live poller ticks on a timer regardless of whether new data
    arrived, and anomaly/engine.py's own poll cadence matters for
    detection latency)."""
    if not ticks:
        return
    start = min(t["observed_at"] for t in ticks)
    end = max(t["observed_at"] for t in ticks)

    buckets = defaultdict(list)
    for t in ticks:
        buckets[t["observed_at"]].append(t)

    poll_time = start
    step = timedelta(seconds=poll_seconds)
    pending = []
    sorted_times = sorted(buckets)
    ti = 0
    while poll_time <= end:
        while ti < len(sorted_times) and sorted_times[ti] <= poll_time:
            pending.extend(buckets[sorted_times[ti]])
            ti += 1
        yield poll_time, pending
        pending = []
        poll_time += step
    # anything left after the last full poll_seconds step (fractional tail)
    if ti < len(sorted_times):
        for t in sorted_times[ti:]:
            pending.extend(buckets[t])
        yield poll_time, pending


def run_scenario(ticks, params):
    """Runs the real fusion engine + both real detectors over `ticks` in
    simulated poll cycles. Returns a list of detected anomaly dicts:
    {kind, track_id, external_id, detected_at}."""
    # Same flat-earth reference the live engine would pick: the center of
    # the data, not geo.py's NYC default.
    if ticks:
        fusion_geo.set_reference(
            sum(t["lat"] for t in ticks) / len(ticks),
            sum(t["lon"] for t in ticks) / len(ticks),
        )
    tracks = {}
    callsign_to_track = {}
    kinematic_state = {}
    open_went_dark = set()
    detections = []

    for poll_time, batch in _poll_cycles(ticks, params["poll_seconds"]):
        adsb, radar = [], []
        for row in batch:
            x, y = fusion_geo.to_local(row["lat"], row["lon"])
            r_std = params["r_adsb_std_m"] if row["source"] == "adsb" else params["r_radar_std_m"]
            tick = {
                "raw_id": row["id"], "external_id": row["external_id"],
                "x": x, "y": y, "observed_at": row["observed_at"],
                "r_std": r_std, "source": row["source"],
                # Live fusion seeds new tracks with the reported velocity.
                "v0": fusion_engine.reported_velocity(row),
            }
            (adsb if row["source"] == "adsb" else radar).append(tick)

        identified = [t for t in adsb if t["external_id"] is not None]
        anonymous = [t for t in adsb if t["external_id"] is None] + radar

        events, touched = [], set()
        if identified:
            fusion_engine.apply_identified(
                identified, tracks, callsign_to_track, params["accel_std"], events, touched
            )
        if anonymous:
            fusion_engine.apply_anonymous(
                anonymous, tracks, params["accel_std"], params["gate_dist_m"],
                params["gate_staleness_s"], events, touched
            )
        fusion_engine.prune_stale(tracks, callsign_to_track, poll_time, params["track_ttl_s"])

        snapshot = [
            {"track_id": t.id, "external_id": t.external_id, "adsb_hits": t.adsb_hits,
             "last_adsb_at": t.last_adsb_at, "last_radar_at": t.last_radar_at}
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
            events, kinematic_state, params["sigma_mult"], params["consecutive"]
        )
        tracks_by_id = {t.id: t for t in tracks.values()}
        for a in kinem_detected:
            a["external_id"] = tracks_by_id.get(a["track_id"]).external_id if tracks_by_id.get(a["track_id"]) else None
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


def sweep(kind, ticks, params, latency_budget_s):
    candidates = eligible_external_ids(ticks)
    results = []
    all_false_positives = []
    skipped = []

    for eid in candidates:
        try:
            if kind == "went_dark":
                modified, gt = inject.inject_went_dark(ticks, eid)
            else:
                modified, gt = inject.inject_kinematic_jump(ticks, eid)
        except ValueError as e:
            skipped.append((eid, str(e)))
            continue

        detections = run_scenario(modified, params)
        tp, fps = score(detections, gt, latency_budget_s)
        results.append({"external_id": eid, "true_positive": tp})
        all_false_positives.extend(fps)

    n = len(results)
    hits = [r for r in results if r["true_positive"]]
    recall = len(hits) / n if n else float("nan")
    latencies = [h["true_positive"]["latency_s"] for h in hits]
    fp_opportunities = sum(max(0, len(candidates) - 1) for _ in results)
    fpr = len(all_false_positives) / fp_opportunities if fp_opportunities else float("nan")

    print(f"kind={kind} window candidates={len(candidates)} scored={n} skipped={len(skipped)}")
    for eid, reason in skipped:
        print(f"  skipped {eid}: {reason}")
    print(f"recall: {len(hits)}/{n} = {recall:.1%}" if n else "recall: n/a (no eligible tracks)")
    if latencies:
        print(f"detection latency: mean={sum(latencies)/len(latencies):.1f}s max={max(latencies):.1f}s")
    print(f"false positives: {len(all_false_positives)} / {fp_opportunities} opportunities "
          f"= {fpr:.1%}" if fp_opportunities else "false positives: n/a")
    return {"recall": recall, "latencies": latencies, "fpr": fpr,
            "n": n, "false_positives": len(all_false_positives)}


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
        sp.add_argument("--latency-budget-s", type=float, default=15.0)
    return p.parse_args()


def window_interval(ticks):
    """Median per-aircraft ADS-B revisit interval within the window -- the
    same quantity the live engines measure from the database."""
    last, gaps = {}, []
    for t in sorted((t for t in ticks if t["source"] == "adsb"), key=lambda t: t["observed_at"]):
        key = t.get("icao24") or t["external_id"]
        if key in last:
            gap = (t["observed_at"] - last[key]).total_seconds()
            if 0 < gap < 300:
                gaps.append(gap)
        last[key] = t["observed_at"]
    return sorted(gaps)[len(gaps) // 2] if gaps else None


def scaled_params(ticks):
    interval = window_interval(ticks) or 10.0
    gate, ttl = fusion_engine.scaled_gates(interval)
    silence, recency = went_dark_thresholds(interval)
    print(f"window adsb interval {interval:.0f}s -> gate {gate:.0f}s, ttl {ttl:.0f}s, "
          f"silence {silence:.0f}s, radar recency {recency:.0f}s")
    return {**DEFAULTS, "gate_staleness_s": gate, "track_ttl_s": ttl,
            "adsb_silence_s": silence, "radar_recency_s": recency}


def main():
    args = parse_args()
    ticks = ingestion_db.ticks_in_window(args.start, args.end)
    if not ticks:
        print("no ticks in that window")
        return
    params = scaled_params(ticks)
    kind = "went_dark" if args.kind == "went_dark" else "kinematic_inconsistency"

    if args.mode == "sweep":
        sweep(args.kind, ticks, params, args.latency_budget_s)
    else:
        if args.kind == "went_dark":
            modified, gt = inject.inject_went_dark(ticks, args.external_id)
        else:
            modified, gt = inject.inject_kinematic_jump(ticks, args.external_id)
        detections = run_scenario(modified, params)
        tp, fps = score(detections, gt, args.latency_budget_s)
        print(f"ground truth: {gt}")
        print(f"true positive: {tp}")
        print(f"false positives ({len(fps)}): {fps}")


if __name__ == "__main__":
    main()
