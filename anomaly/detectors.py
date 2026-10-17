"""
detectors.py — Dark Track / anomaly: the two anomaly rules (weeks 10-11).

Both are plain functions over dicts, not DB queries, so the exact same code
runs live (anomaly/engine.py, reading real fused_tracks/fused_track_events)
and under test (anomaly/harness.py, feeding fusion/engine.py's in-memory
output directly, no DB involved). See docs/PROBLEM.md "Anomaly detection"
for the detection-latency / <5%-false-positive-rate success criteria these
are tuned against, and anomaly/README.md for the measured numbers.
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
        # Ground traffic: OpenSky reports no barometric altitude on the
        # ground, airport ADS-B coverage is patchy, and dozens of taxiing
        # aircraft sit within the association gate of each other -- so a
        # neighbour's radar keeps a silent track "corroborated". Measured on
        # 30 min of NYC: all 27 false went-dark alerts were ground traffic.
        # Going dark only means something airborne.
        alt = t.get("last_adsb_alt_m", MIN_AIRBORNE_ALT_M)
        if alt is None or alt < MIN_AIRBORNE_ALT_M:
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


def went_dark_episodes(tracks, now, open_episodes, adsb_silence_s=30.0, radar_recency_s=10.0):
    """went_dark() with episode state, so one dropout is one alert.

    An episode opens when went_dark() first flags a track and stays open
    until ADS-B actually comes back (the track's last_adsb_at moves past
    where it was when the episode opened) or the track is retired --
    *not* whenever went_dark() momentarily stops flagging it. At 45 s
    cadence with 10% radar dropout, a radar gap past radar_recency_s is
    routine, and closing on it re-alerted the same dark aircraft every
    few minutes (YETI45, Gulf 2026-10-14 03:37: alerts at +54 s, +195 s
    and +303 s for one dropout).

    `open_episodes`: {track_id: last_adsb_at when the episode opened};
    updated in place and returned. Returns (new hits, resolved track
    ids, open_episodes)."""
    by_id = {t["track_id"]: t for t in tracks}
    new = [h for h in went_dark(tracks, now, adsb_silence_s, radar_recency_s)
           if h["track_id"] not in open_episodes]
    for h in new:
        open_episodes[h["track_id"]] = by_id[h["track_id"]]["last_adsb_at"]
    resolved = [
        tid for tid, adsb_at in open_episodes.items()
        if tid not in by_id
        or (by_id[tid]["last_adsb_at"] is not None
            and (adsb_at is None or by_id[tid]["last_adsb_at"] > adsb_at))
    ]
    for tid in resolved:
        del open_episodes[tid]
    return new, resolved, open_episodes


# Below this (or no altitude at all) a track's last ADS-B fix counts as
# on the ground / on final, and went_dark() ignores it.
MIN_AIRBORNE_ALT_M = 100.0


def went_dark_thresholds(interval_s):
    """(adsb_silence_s, radar_recency_s) for a given ADS-B update interval.
    Silence must exceed one normal revisit plus jitter, or every aircraft
    "goes dark" between polls; radar recency must span a revisit, or an
    open episode flaps closed/open each cycle (each reopen scoring as a new
    alert). Floors are the ~10s-polling values (30s / 10s). Measured on
    45s-cadence Gulf data with the unscaled 30s/10s: went-dark recall
    1.6%."""
    return max(30.0, 1.5 * interval_s), max(10.0, 1.3 * interval_s)


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
    `state`: {"streaks": {(track_id, source): int}, "open": {track_id:
    normal updates since}}, mutated in place and also returned (start
    with {}) -- caller keeps this across polls (in memory,
    same pattern as fusion/engine.py's own `tracks` dict) so a streak isn't
    lost between poll cycles. Lost on process restart -- see
    anomaly/README.md known limitations.

    Edge-triggered both ways, one episode per track: emits `detected` the
    moment any source's streak *reaches* `consecutive` while the track has
    no open episode, and `resolved` once the track has had `consecutive`
    normal updates in a row.

    Returns (detected, resolved, state).
    """
    # Streaks are counted per (track, source) -- ADS-B and radar residuals
    # aren't comparable -- but the *episode* is per track, and closes only
    # after `consecutive` normal updates in a row (any source), the same
    # bar it took to open. Per-(track, source) episodes that closed on one
    # normal update re-alerted the same aircraft throughout a maneuver:
    # on NYC 2026-10-10, all 98 "induced" kinematic alerts in 40 injected
    # runs were the injected target itself.
    streaks = state.setdefault("streaks", {})
    open_tracks = state.setdefault("open", {})  # track_id -> normal updates since
    detected = []
    resolved = []
    for e in events:
        tid = e["track_id"]
        key = (tid, e["source"])
        normalized = e["residual_m"] / e["innovation_std_m"]

        if normalized > sigma_mult:
            streaks[key] = streaks.get(key, 0) + 1
            if tid in open_tracks:
                open_tracks[tid] = 0
            elif streaks[key] >= consecutive:
                open_tracks[tid] = 0
                detected.append({
                    "track_id": tid,
                    "external_id": None,
                    "kind": "kinematic_inconsistency",
                    "detected_at": e["observed_at"],
                    "metric": normalized,
                    "source": e["source"],
                })
        else:
            streaks[key] = 0
            if tid in open_tracks:
                open_tracks[tid] += 1
                if open_tracks[tid] >= consecutive:
                    del open_tracks[tid]
                    resolved.append({
                        "track_id": tid,
                        "kind": "kinematic_inconsistency",
                        "resolved_at": e["observed_at"],
                    })
    return detected, resolved, state
