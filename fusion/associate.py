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

import math

import numpy as np
from scipy.optimize import linear_sum_assignment

UNGATED_COST = 1e9
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

    cost = [[UNGATED_COST] * len(track_ids) for _ in ticks]
    for i, tick in enumerate(ticks):
        for j, tid in enumerate(track_ids):
            track = tracks[tid]
            age = (tick["observed_at"] - track.last_update).total_seconds()
            # age can go slightly negative when a batch processes several
            # ADS-B updates for the same track before its radar-derived
            # ticks (radar_sim's latency jitter puts them a few seconds
            # later in real time, but a big backfill can still process the
            # track's *later* ADS-B ticks first). Kalman peek_predict
            # already clamps negative dt to 0 and uses the current
            # estimate, so only reject when the mismatch is larger than
            # the staleness gate would tolerate in either direction.
            if abs(age) > max_staleness_s:
                continue
            x_pred, P_pred = track.kf.peek_predict(age)
            innovation = np.array([tick["x"] - x_pred[0], tick["y"] - x_pred[1]])
            dist = float(math.hypot(*innovation))
            if dist > max_dist_m:
                S = P_pred[:2, :2] + np.eye(2) * tick["r_std"] ** 2
                if float(innovation @ np.linalg.solve(S, innovation)) > CHI2_GATE:
                    continue
            cost[i][j] = dist

    row_ind, col_ind = linear_sum_assignment(cost)

    result = [None] * len(ticks)
    for i, j in zip(row_ind, col_ind):
        if cost[i][j] < UNGATED_COST:
            result[i] = track_ids[j]
    return result
