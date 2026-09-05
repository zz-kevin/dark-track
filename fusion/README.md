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
| `kalman.py` | The per-track constant-velocity Kalman filter |
| `track.py` | Wraps a filter with identity/recency bookkeeping + confidence heuristic |
| `associate.py` | Gating + Hungarian assignment for anonymous ticks |
| `engine.py` | Main loop tying it all together |

## Running it

```
python3 db.py         # creates fused_tracks / fused_track_events
python3 engine.py      # run alongside ingestion/opensky_poll.py + radar_sim.py
```

## Status

- [x] Weeks 5–9 — Kalman filter, gating, Hungarian association, track
      maintenance, confidence heuristic. See `docs/FUSION.md` for the
      reasoning and known limitations (batch-order edge case, no
      smoothing, no re-ID after track loss, no velocity-aware gating).
