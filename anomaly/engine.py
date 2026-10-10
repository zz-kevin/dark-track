#!/usr/bin/env python3
"""
engine.py — Dark Track / anomaly: main detection loop.

Polls the fused picture written by fusion/engine.py (fused_tracks,
fused_track_events; same DATABASE_URL) and runs both detectors from
detectors.py, writing edge-triggered alerts to anomaly_events. Run
`python3 db.py` once first to create that table; run this alongside
fusion/engine.py.

Each poll cycle, in order:

  1. Fetch the current fused picture and run went_dark() against it. Diff
     the resulting flagged-track-id set against what was open last cycle:
     newly flagged -> insert (skipped if the DB already has it open, so a
     restart doesn't double-insert); no-longer-flagged (ADS-B resumed, or
     the track was retired out of fused_tracks entirely) -> resolve.
  2. Fetch fused_track_events since the last cursor and run
     kinematic_inconsistency() against them, which is itself
     edge-triggered (see detectors.py) and returns both new detections and
     resolutions directly.
"""

import argparse
import time
from datetime import datetime, timezone

import db
from detectors import kinematic_inconsistency, went_dark


def run(poll_seconds, adsb_silence_s, radar_recency_s,
        sigma_mult, consecutive, since_id_events):
    cursor = since_id_events if since_id_events is not None else db.latest_event_id()
    kinematic_state = {}
    open_went_dark = db.open_track_ids("went_dark")

    print(
        f"Watching fused picture for went-dark (silence>{adsb_silence_s}s, "
        f"radar<{radar_recency_s}s) and kinematic inconsistency "
        f"({sigma_mult}x sigma, {consecutive} consecutive), every "
        f"{poll_seconds}s. Ctrl+C to stop.\n"
    )

    while True:
        try:
            tracks = db.fetch_tracks()
            tracks_by_id = {t["track_id"]: t for t in tracks}
            now = max(
                (t["updated_at"] for t in tracks),
                default=datetime.now(timezone.utc),
            )

            dark_hits = went_dark(tracks, now, adsb_silence_s, radar_recency_s)
            currently_open = {h["track_id"] for h in dark_hits}
            for h in dark_hits:
                if h["track_id"] not in open_went_dark:
                    db.insert_anomaly_event(**h)
            for tid in open_went_dark - currently_open:
                db.resolve_episode(tid, "went_dark", now)
            open_went_dark = currently_open

            events = db.events_since(cursor)
            if events:
                cursor = max(e["id"] for e in events)
            kinem_detected, kinem_resolved, kinematic_state = kinematic_inconsistency(
                events, kinematic_state, sigma_mult, consecutive
            )
            for a in kinem_detected:
                external_id = tracks_by_id.get(a["track_id"], {}).get("external_id")
                db.insert_anomaly_event(
                    track_id=a["track_id"], external_id=external_id, kind=a["kind"],
                    detected_at=a["detected_at"], metric=a["metric"], source=a["source"],
                )
            for r in kinem_resolved:
                db.resolve_episode(r["track_id"], r["kind"], r["resolved_at"])

            if dark_hits or kinem_detected or kinem_resolved:
                print(
                    f"[{now.strftime('%H:%M:%S')}] went_dark: {len(dark_hits)} open, "
                    f"kinematic: +{len(kinem_detected)} new -{len(kinem_resolved)} resolved"
                )
        except Exception as e:
            print(f"anomaly tick failed: {e}")
            raise

        try:
            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("\nstopped.")
            break


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--poll-seconds", type=float, default=3.0)
    p.add_argument("--adsb-silence-s", type=float, default=30.0,
                    help="ADS-B silence, seconds, before a still-radar-corroborated "
                         "track is flagged went-dark. 10s (naive poll-interval-plus-"
                         "margin guess) was swamped by real OpenSky poll jitter in "
                         "testing -- see anomaly/README.md")
    p.add_argument("--radar-recency-s", type=float, default=10.0,
                    help="radar contact must be this recent for went-dark to fire")
    p.add_argument("--sigma-mult", type=float, default=4.0,
                    help="normalized-residual threshold for kinematic inconsistency")
    p.add_argument("--consecutive", type=int, default=2,
                    help="consecutive over-threshold updates (same source) required to alert")
    p.add_argument("--since-id-events", type=int, default=None)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(
        poll_seconds=args.poll_seconds,
        adsb_silence_s=args.adsb_silence_s,
        radar_recency_s=args.radar_recency_s,
        sigma_mult=args.sigma_mult,
        consecutive=args.consecutive,
        since_id_events=args.since_id_events,
    )
