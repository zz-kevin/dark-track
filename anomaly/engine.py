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
from detectors import kinematic_inconsistency, went_dark, went_dark_thresholds


def run(poll_seconds, adsb_silence_s, radar_recency_s,
        sigma_mult, consecutive, since_id_events):
    interval = db.adsb_update_interval()
    auto_silence, auto_recency = went_dark_thresholds(interval or 10.0)
    adsb_silence_s = adsb_silence_s if adsb_silence_s is not None else auto_silence
    radar_recency_s = radar_recency_s if radar_recency_s is not None else auto_recency
    print(f"adsb update interval: "
          f"{f'{interval:.0f}s (measured)' if interval else 'unknown, assuming 10s'}")
    cursor = since_id_events if since_id_events is not None else db.latest_event_id()
    kinematic_state = {}
    open_went_dark = db.open_track_ids("went_dark")

    print(
        f"Watching fused picture for went-dark (silence>{adsb_silence_s:.0f}s, "
        f"radar<{radar_recency_s:.0f}s) and kinematic inconsistency "
        f"({sigma_mult}x sigma, {consecutive} consecutive), every "
        f"{poll_seconds}s. Ctrl+C to stop.\n"
    )

    # Quiet unless something fires -- so a heartbeat line every
    # HEARTBEAT_S shows it's alive when you're watching its output.
    last_heartbeat = 0.0

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
            new_dark = [h for h in dark_hits if h["track_id"] not in open_went_dark]
            for h in new_dark:
                db.insert_anomaly_event(**h)
            resolved_dark = open_went_dark - currently_open
            for tid in resolved_dark:
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

            # Only on change -- an open episode used to reprint every poll.
            if new_dark or resolved_dark or kinem_detected or kinem_resolved:
                print(
                    f"[{now.astimezone(timezone.utc).strftime('%H:%M:%S')}] "
                    f"went_dark: +{len(new_dark)} new -{len(resolved_dark)} resolved "
                    f"({len(open_went_dark)} open"
                    + (f": {', '.join(str(h['external_id']) for h in new_dark)}" if new_dark else "")
                    + f"), kinematic: +{len(kinem_detected)} new -{len(kinem_resolved)} resolved"
                )
            elif time.time() - last_heartbeat >= HEARTBEAT_S:
                print(
                    f"[{now.astimezone(timezone.utc).strftime('%H:%M:%S')}] "
                    f"watching {len(tracks)} tracks -- no new anomalies"
                    + (f", {len(open_went_dark)} went-dark still open" if open_went_dark else "")
                )
                last_heartbeat = time.time()
        except Exception as e:
            print(f"anomaly tick failed: {e}")
            raise

        try:
            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("\nstopped.")
            break


HEARTBEAT_S = 60


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--poll-seconds", type=float, default=3.0)
    p.add_argument("--adsb-silence-s", type=float, default=None,
                    help="ADS-B silence, seconds, before a still-radar-corroborated "
                         "track is flagged went-dark. 10s (naive poll-interval-plus-"
                         "margin guess) was swamped by real OpenSky poll jitter in "
                         "testing -- see anomaly/README.md. Default: scaled to the "
                         "measured ADS-B interval (detectors.went_dark_thresholds)")
    p.add_argument("--radar-recency-s", type=float, default=None,
                    help="radar contact must be this recent for went-dark to fire "
                         "(default: scaled to the measured ADS-B interval)")
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
