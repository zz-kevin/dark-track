"""
associate.py — Dark Track / fusion: gating + association for anonymous
ticks (radar contacts, and any ADS-B tick that arrives without a
callsign).

Gating narrows candidates before association ever runs: a tick can only
match a track if the track was recently updated (max_staleness_s) and the
track's *predicted* position at the tick's observed_at is within
max_dist_m of the tick's measured position. Association then resolves,
across a whole batch of ticks at once, which track-tick pairing
minimizes total distance -- the Hungarian algorithm (via
scipy.optimize.linear_sum_assignment), not greedy nearest-neighbor.

The distance gate is the larger of a fixed radius (max_dist_m) and a
covariance gate on the filter's own innovation covariance S = HPH^T + R
(squared Mahalanobis distance <= CHI2_GATE). The fixed radius alone was
tuned for radar contacts that arrive seconds after the track's last
update; when ADS-B drops on a 45s-cadence feed, the next radar contact is
judged against a 45s prediction whose real error was measured at median
954m / p95 3.6km -- an 800m radius rejected most of them, so the track
never got the radar corroboration the went-dark detector needs. The
covariance gate widens exactly when the filter is genuinely unsure and
never makes the gate tighter than max_dist_m.

Why Hungarian over greedy nearest-neighbor: radar_sim derives all of a
poll cycle's contacts from the same OpenSky snapshot, so multiple
ambiguous ticks routinely land in the same batch. Greedy assignment
(claim the globally closest pair, repeat) can steal a good match away
from a tick that had no other reasonable candidate; Hungarian solves for
the assignment that minimizes total distance across the whole batch,
which matters most exactly when two aircraft are close together -- the
case greedy handles worst and this project's radar noise (150m std) makes
common in busy airspace.
"""

from datetime import datetime, timezone

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.special import logsumexp

UNGATED_COST = 1e9
# "likelihood" or "euclidean" -- see associate()'s cost comment.
COST = "likelihood"
# Altitude in the likelihood and gate (altitude.py). Chi-square, 1 degree
# of freedom, 99.9%; vague prior for tracks with no height yet. Off: it
# fixed a traced holding-stack miss but was neutral on six validation
# windows (docs/FUSION.md), so it's an opt-in.
USE_ALTITUDE = False
ALT_CHI2_GATE = 10.83
UNKNOWN_ALT_STD = 3000.0
# Chi-square, 2 degrees of freedom, 99.9%: a true match falls outside this
# only 1 time in 1000 if the filter's covariance is honest.
CHI2_GATE = 13.82


def associate(ticks, tracks, max_dist_m, max_staleness_s):
    """ticks: list of dicts with 'x', 'y', 'observed_at'.
    tracks: dict of track_id -> Track.
    Returns a list, parallel to `ticks`, of matched track_id or None."""
    track_ids = list(tracks.keys())
    if not track_ids or not ticks:
        return [None] * len(ticks)

    # Each track is predicted once, with its own motion model, to a base
    # time -- the batch's earliest tick, or its own last update if later --
    # and every tick is then gated against all tracks at once as arrays,
    # extrapolating the last (sub-second, within an association slice)
    # stretch at constant velocity. For the constant-velocity filter
    # that's exact (white-noise-acceleration noise composes over time);
    # for the IMM filter it's exact up to the slice width. The per-pair
    # version was the slowest step of a fusion cycle (p95 360 ms on a
    # 45s-cadence Gulf burst of ~120 tracks).
    trk = [tracks[tid] for tid in track_ids]
    t_min = min(t["observed_at"] for t in ticks).timestamp()
    last = np.array([t.last_update.timestamp() for t in trk])
    base = np.maximum(t_min, last)
    # Each track's prediction as weighted Gaussian components: one for the
    # constant-velocity filter, one per motion model for the IMM filter.
    # Arrays are [component, track, ...].
    comps = [t.kf.peek_predict_components(b - lu) for t, b, lu in zip(trk, base, last)]
    W = np.array([[c[k][0] for c in comps] for k in range(len(comps[0]))])
    X = np.array([[c[k][1] for c in comps] for k in range(len(comps[0]))])
    P = np.array([[c[k][2] for c in comps] for k in range(len(comps[0]))])
    q = np.array([t.kf.accel_std ** 2 for t in trk])
    logW = np.log(np.maximum(W, 1e-300))
    # Altitude predicted at each track's base time (the sub-second rest of
    # the slice is ignored: < 20 m even climbing). NaN = no estimate yet.
    alt = [t.alt.peek(datetime.fromtimestamp(b, tz=timezone.utc))
           if USE_ALTITUDE and t.alt else (np.nan, np.nan)
           for t, b in zip(trk, base)]
    H = np.array([a[0] for a in alt])
    VH = np.array([a[1] for a in alt])

    cost = np.full((len(ticks), len(track_ids)), UNGATED_COST)
    for i, tick in enumerate(ticks):
        t = tick["observed_at"].timestamp()
        age = t - last
        # age can go slightly negative when a batch processes several
        # ADS-B updates for the same track before its radar-derived
        # ticks (radar_sim's latency jitter puts them a few seconds
        # later in real time, but a big backfill can still process the
        # track's *later* ADS-B ticks first). Kalman peek_predict
        # already clamps negative dt to 0 and uses the current
        # estimate, so only reject when the mismatch is larger than
        # the staleness gate would tolerate in either direction.
        fresh = np.abs(age) <= max_staleness_s
        dt = np.maximum(t - base, 0.0)
        dx = tick["x"] - (X[..., 0] + X[..., 2] * dt)
        dy = tick["y"] - (X[..., 1] + X[..., 3] * dt)
        dist = np.hypot((W * dx).sum(0), (W * dy).sum(0))  # from the blended mean
        # S = position block of F P F^T + Q, plus R.
        qd = q * dt ** 4 / 4 + tick["r_std"] ** 2
        sxx = P[..., 0, 0] + 2 * dt * P[..., 0, 2] + dt ** 2 * P[..., 2, 2] + qd
        syy = P[..., 1, 1] + 2 * dt * P[..., 1, 3] + dt ** 2 * P[..., 3, 3] + qd
        sxy = P[..., 0, 1] + dt * (P[..., 0, 3] + P[..., 2, 1]) + dt ** 2 * P[..., 2, 3]
        det = sxx * syy - sxy ** 2
        mahal2 = (syy * dx ** 2 - 2 * sxy * dx * dy + sxx * dy ** 2) / det
        gated = fresh & ((dist <= max_dist_m) | (mahal2.min(0) <= CHI2_GATE))
        alt_term = 0.0
        if USE_ALTITUDE and tick.get("alt") is not None:
            # Height as a third, independent dimension of the likelihood. A
            # track with no altitude estimate is scored under a vague prior,
            # so it doesn't win just by having nothing to disagree with.
            known = ~np.isnan(H)
            sz = np.where(known, VH, UNKNOWN_ALT_STD ** 2) + tick["alt_std"] ** 2
            dz = np.where(known, tick["alt"] - H, 0.0)
            alt_term = dz ** 2 / sz + np.log(sz)
            gated &= dz ** 2 / sz <= ALT_CHI2_GATE
        if COST == "likelihood":
            # Negative log-likelihood of the tick under each track's
            # predicted mixture (x2, constants dropped; a single Gaussian
            # gives Mahalanobis^2 + log det S). Plain distance let a
            # neighbour updated seconds ago, 600 m away with a tight
            # covariance, take a radar contact from a just-gone-dark track
            # whose 45s prediction is ~1 km off but ~3 km uncertain. The
            # mixture, not its moment-matched blend, matters for the IMM:
            # over a 45s gap the blend drifts halfway back to straight
            # flight, while the turn component alone stays on the turn.
            nll = -2.0 * logsumexp(logW - 0.5 * (mahal2 + np.log(det)), axis=0) + alt_term
            cost[i, gated] = nll[gated]
        else:
            cost[i, gated] = dist[gated]

    row_ind, col_ind = linear_sum_assignment(cost)

    result = [None] * len(ticks)
    for i, j in zip(row_ind, col_ind):
        if cost[i, j] < UNGATED_COST:
            result[i] = track_ids[j]
    return result
