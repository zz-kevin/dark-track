"""
detectors.py — Dark Track / anomaly: the two anomaly rules (weeks 10-11).

Both are plain functions over dicts, not DB queries, so the exact same code
runs live (anomaly/engine.py, reading real fused_tracks/fused_track_events)
and under test (anomaly/harness.py, feeding fusion/engine.py's in-memory
output directly, no DB involved). See docs/PROBLEM.md "Anomaly detection"
for the 15s-detection / <5%-false-positive-rate success criteria these are
tuned against, and anomaly/README.md for the measured numbers.
"""


def went_dark(tracks, now, adsb_silence_s=30.0, radar_recency_s=10.0):
    """Flags tracks where ADS-B has been silent for more than
    adsb_silence_s while the non-cooperative (radar) contact is still
    corroborating within the last radar_recency_s -- docs/PROBLEM.md's
    "transponder drops while the radar-like contact keeps flying the same
    profile."

    Only considers tracks that were previously seen on ADS-B at all
    (adsb_hits > 0) -- a track that has never carried a callsign is just an
    unidentified radar contact, not something that "went dark".

    `tracks`: iterable of dicts with track_id, external_id, adsb_hits,
    last_adsb_at, last_radar_at (the fused_tracks columns).

    Stateless: doesn't know which tracks were already flagged. Whether an
    episode is "new" (edge-triggered dedup against anomaly_events) is the
    caller's job -- anomaly/engine.py checks the DB, anomaly/harness.py
    checks an in-memory set -- since "already open" is a fact about
    anomaly_events, not about the fused picture this function reads.

    Returns a list of dicts: track_id, external_id, kind, detected_at,
    metric (the ADS-B silence gap in seconds), source ('radar', the
    corroborating source).
    """
    out = []
    for t in tracks:
        if not t.get("adsb_hits") or t["last_adsb_at"] is None or t["last_radar_at"] is None:
            continue
        adsb_gap = (now - t["last_adsb_at"]).total_seconds()
        radar_gap = (now - t["last_radar_at"]).total_seconds()
        if adsb_gap > adsb_silence_s and radar_gap <= radar_recency_s:
            out.append({
                "track_id": t["track_id"],
                "external_id": t["external_id"],
                "kind": "went_dark",
                "detected_at": now,
                "metric": adsb_gap,
                "source": "radar",
            })
    return out


def kinematic_inconsistency(events, state, sigma_mult=4.0, consecutive=2):
    """Flags a track whose Kalman innovation (residual_m, already computed
    per-update by fusion/track.py's Track.apply) is more than sigma_mult
    times the filter's own innovation std dev (innovation_std_m -- sqrt of
    S = HPH^T + R at that update, also computed per-update by
    fusion/kalman.py) for `consecutive` updates in a row from the same
    source -- i.e. the constant-velocity + accel_std process model can't
    explain what it's seeing, not just one noisy tick. This is
    anomaly/README.md's "speed/heading change outside what the vehicle
    type should plausibly do" rule, expressed via the filter's own
    statistical consistency check rather than a hand-picked turn-rate limit
    (see docs/ARCHITECTURE.md's Kalman-over-alpha-beta decision -- same
    reasoning applies here: R already encodes per-source trust).

    Normalizing by innovation_std_m rather than a fixed per-source R std
    dev matters because S grows with elapsed dt via process_noise -- a
    track that hasn't been updated in a while (long real-world ADS-B/radar
    gaps are common, see anomaly/README.md) legitimately has a wider
    predicted-position uncertainty, and a normal-sized residual after that
    gap should not look anomalous. Normalizing by fixed R conflated "long
    gap, normal reacquisition" with "genuine maneuver" -- see
    anomaly/README.md's measured false-positive numbers before/after this
    fix.

    `events`: fused_track_events rows in id/time order -- id, track_id,
    source, observed_at, residual_m, innovation_std_m.
    `state`: {(track_id, source): {"count": int, "open": bool}}, mutated in
    place and also returned -- caller keeps this across polls (in memory,
    same pattern as fusion/engine.py's own `tracks` dict) so a streak isn't
    lost between poll cycles. Lost on process restart -- see
    anomaly/README.md known limitations.

    Edge-triggered both ways: emits a `detected` entry the moment a streak
    *reaches* `consecutive` (not on every tick after), and a `resolved`
    entry the moment a tick with normal residual breaks an open streak.

    Returns (detected, resolved, state).
    """
    detected = []
    resolved = []
    for e in events:
        key = (e["track_id"], e["source"])
        s = state.setdefault(key, {"count": 0, "open": False})
        normalized = e["residual_m"] / e["innovation_std_m"]

        if normalized > sigma_mult:
            s["count"] += 1
            if s["count"] >= consecutive and not s["open"]:
                s["open"] = True
                detected.append({
                    "track_id": e["track_id"],
                    "external_id": None,
                    "kind": "kinematic_inconsistency",
                    "detected_at": e["observed_at"],
                    "metric": normalized,
                    "source": e["source"],
                })
        else:
            if s["open"]:
                resolved.append({
                    "track_id": e["track_id"],
                    "kind": "kinematic_inconsistency",
                    "resolved_at": e["observed_at"],
                })
            s["count"] = 0
            s["open"] = False
    return detected, resolved, state
