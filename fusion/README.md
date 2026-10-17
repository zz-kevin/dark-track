# fusion/

The technical core of the project (weeks 5–9). Turns two noisy,
uncorrelated track streams into one confident picture per real-world entity.

- Per-track state estimation (constant-velocity Kalman filter)
- Gating — which track pairs are even candidates for the same object
- Association — Hungarian algorithm (`scipy.optimize.linear_sum_assignment`),
  resolved per poll cycle
- Conflict resolution — source disagreement, dropout, re-appearance under a
  new ID
- Confidence scoring on the emitted fused track

Design reasoning and rejected alternatives are in
[`../docs/FUSION.md`](../docs/FUSION.md), not just in code comments.

## Files

| File | Purpose |
|---|---|
| `schema.sql` | `fused_tracks` (current picture) + `fused_track_events` (append-only update log) |
| `db.py` | Reads `raw_ticks` (shared with `ingestion/`), writes the fused picture |
| `geo.py` | lat/lon <-> local ENU meters, so the filter works in plain Euclidean space |
| `kalman.py` | The per-track constant-velocity Kalman filter (the default) |
| `altitude.py` | Per-track altitude estimate (with ADS-B glitch rejection); used in association only when `associate.USE_ALTITUDE` is on |
| `imm.py` | Opt-in interacting-multiple-model filter (constant velocity + coordinated turn); measured, not used -- see docs/FUSION.md |
| `track.py` | Wraps a filter with identity/recency bookkeeping + confidence heuristic |
| `associate.py` | Gating + Hungarian assignment for anonymous ticks |
| `engine.py` | Main loop tying it all together |
| `simulate.py` | The same fusion calls run in memory over a stored window -- shared by the harness, accuracy, bench and the API's replay |
| `accuracy.py` | Association accuracy against radar ground truth (docs/PROBLEM.md's 300 m / 5 s / 90% target) |
| `bench.py` | Throughput + tick -> fused_tracks latency, real Postgres writes into a scratch DB |

## Running it

```
python3 db.py         # creates fused_tracks / fused_track_events
python3 engine.py      # run alongside ingestion/opensky_poll.py + radar_sim.py
python3 engine.py --region gulf   # when two pollers are running at once
```

## Measuring it

```
python3 accuracy.py 2026-10-14T06:15:00+00:00 2026-10-14T06:30:00+00:00
python3 bench.py 2026-10-14T06:15:00+00:00 2026-10-14T06:30:00+00:00 --copies 4
```

Add `--region nyc|gulf` if both pollers ran during the window. `bench.py`
writes to `dark_track_bench` (created if missing), never the live tables.

**Accuracy** (aircraft with >= 90% of radar contacts on their own track,
within 300 m, within 5 s), on validation windows: Gulf 96.7-100%,
NYC 81.2% (93.0% airborne -- the gap is taxiing traffic). Every window,
and the failure analysis, in [`../docs/FUSION.md`](../docs/FUSION.md).

**Throughput** (Gulf 2026-10-14 06:15-06:30, 0.25s poll):

| Copies | Concurrent tracks (p50 / max) | Cycle p95 | Tick -> fused_tracks p95 / max | Under 500 ms |
|---|---|---|---|---|
| 1 | 114 / 128 | 26 ms | 239 / 284 ms | 100% |
| 2 | 228 / 259 | 30 ms | 241 / 312 ms | 100% |
| 4 | 456 / 521 | 37 ms | 248 / 352 ms | 100% |

(Re-run after the likelihood cost and again after the velocity
measurement, 4 copies: cycle p95 40 ms, tick p95 249 ms / max 310 ms,
100% under 500 ms. After mixture-likelihood association, which predicts
each track once per slice: cycle p95 50 ms, tick p95 259 ms / max
331 ms, 100% under 500 ms.)

Before this round (3s poll, a connection per upserted track, per-pair
gating) the 1-copy run had cycle p95 969 ms and 10% of ticks under
500 ms. NYC (11s cadence) at 4 copies, ~240 tracks: 100% under 500 ms,
max 298 ms. Tick latency counts from the tick's `observed_at` to its
cycle's commit; it doesn't include OpenSky's own delay or the poller's
interval, which no fusion change can affect.

## Status

- [x] Weeks 5–9 — Kalman filter, gating, Hungarian association, track
      maintenance, confidence heuristic. See `docs/FUSION.md` for the
      reasoning and known limitations (batch-order edge case, no
      smoothing, no re-ID after track loss, no velocity-aware gating).
- [x] Weeks 14-15 (early) -- measured against docs/PROBLEM.md: accuracy
      (`accuracy.py`) and throughput (`bench.py`); time-sliced
      association, vectorized gating, batched writes, 0.25s poll.
- [x] Likelihood (Mahalanobis + log det S) assignment cost and 2.5x
      staleness gate, from tracing held-out went-dark misses; scored on
      untouched validation windows (docs/FUSION.md, anomaly/README.md).
- [x] ADS-B velocity as a filter measurement (airborne), ADS-B position
      noise 50 m; validated on fresh windows (docs/FUSION.md).
- [x] Ground traffic: 93% of NYC ground swaps are Bayes-limited (the
      contact really is likelier under the other aircraft) -- closed as
      a sensor limit. Velocity time offset: recorded, measured worse on
      Gulf, off pending an NYC capture with `velocity_at`.
- [x] Interacting-multiple-model filter (`imm.py`, opt-in): no gain on
      the turn-dropout window and kinematic detection collapses, so the
      default stays constant velocity. Association now scores per-model
      mixtures (identical for one model). See docs/FUSION.md.
- [x] Radar height error in radar_sim; altitude in association as an
      opt-in (fixed the holding-stack miss, neutral on six validation
      windows). Went-dark scoring no longer counts coverage gaps.
- [x] Velocity time offset on NYC: no effect (NYC offsets median 0 s);
      stays off.
- [ ] Open: same-level formations (FRS41/FRS42) -- inside radar's
      resolution in every dimension; nothing in the current sensor model
      separates them.
