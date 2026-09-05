# Architecture

_Status: sketch — flesh this out in week 1 once the data flow is proven end to
end. Keep it to one page for as long as possible._

## Data flow

```
OpenSky Network API  ──┐
  (real, cooperative,  │
   has ID)             ▼
                  ingestion/  ──►  storage (Postgres/PostGIS)  ──►  replay
                        ▲                     │
Synthetic radar gen  ───┘                     ▼
  (derived from truth,                  fusion/
   no ID, noisy)                    (Kalman filter,
                                      gating, association)
                                             │
                                             ▼
                                        anomaly/
                                   (dark-track, kinematic
                                      inconsistency)
                                             │
                                             ▼
                                          api/  ──►  web/
                                                   (map, replay,
                                                    alert panel)
```

## Components

- **ingestion/** — one poller for real OpenSky state vectors, one generator
  for the synthetic secondary sensor. Both write to the same raw-track
  schema so downstream code doesn't care which source a tick came from until
  fusion needs to.
- **fusion/** — the technical core. Per-track filtering, gating (which pairs
  are even candidates), association (nearest-neighbor / Hungarian), conflict
  resolution, confidence scoring.
- **anomaly/** — consumes the fused picture, runs rule-based + statistical
  detectors, exposes a test harness that injects synthetic anomalies into a
  replay run.
- **api/** — thin layer serving fused tracks + anomaly feed to the frontend.
  Not the interesting part of this project — keep it simple.
- **web/** — map view, replay controls, click-to-inspect track history.

## Decisions log

Record real decisions here as they're made, in the format:

> **Decision:** what was chosen
> **Alternatives considered:** what else was on the table
> **Why:** the actual reasoning, including constraints (time, complexity)

> **Decision:** Plain Postgres for `raw_ticks` (lat/lon as `double precision`
> columns), not PostGIS.
> **Alternatives considered:** PostGIS with a `geography(Point)` column and
> spatial indexing.
> **Why:** at this project's scale (single region, a few hundred tracks max)
> a plain lat/lon pair with a btree index on `(source, observed_at)` is
> enough for both ingestion and the gating step in `fusion/`. PostGIS is an
> extra install/ops dependency that only pays for itself once queries need
> real spatial predicates (radius search, polygon containment) — revisit if
> `fusion/`'s gating turns out to need that.

> **Decision:** Radar simulator derives contacts from ticks already written
> to `raw_ticks` by the OpenSky poller (polls the table), rather than
> independently polling OpenSky itself.
> **Alternatives considered:** Have `radar_sim.py` call the OpenSky API
> directly and apply noise inline, same as the poller.
> **Why:** guarantees the two sources are actually derived from the same
> underlying truth (required by `docs/PROBLEM.md`), and decouples the two
> processes' poll cadence — the radar "sensor" can run on its own schedule
> without doubling OpenSky API calls against the anonymous rate limit.

> **Decision:** Radar latency jitter is simulated by writing a
> future-shifted `observed_at` rather than literally sleeping per tick.
> **Alternatives considered:** Real `time.sleep()` before inserting each
> contact.
> **Why:** makes a fixed `--seed` replay run reproduce identical timing
> deterministically, and avoids serializing insert latency across many
> aircraft in one poll cycle.

> **Decision:** Constant-velocity Kalman filter per track, with per-source
> measurement noise (`R_adsb` << `R_radar`), over an alpha-beta filter.
> **Alternatives considered:** alpha-beta filter with separate hand-tuned
> gains per source.
> **Why:** the project's actual point is trusting one source more than the
> other — a Kalman filter's `R` matrix does that natively per update,
> where alpha-beta has no equivalent lever without hand-hacking it. Full
> reasoning in `docs/FUSION.md`.

> **Decision:** Hungarian algorithm (`scipy.optimize.linear_sum_assignment`)
> for associating a poll cycle's anonymous ticks to tracks, not greedy
> nearest-neighbor.
> **Alternatives considered:** greedy nearest-neighbor.
> **Why:** `radar_sim.py` derives a whole poll cycle's contacts from one
> OpenSky snapshot, so ambiguous ticks close together in a busy region are
> common in the same batch — exactly where greedy assignment can steal a
> good match from a tick with no other candidate. Measured cost is
> negligible at this project's track counts.

> **Decision:** Fusion processes each cycle's identified ADS-B ticks
> before batch-associating the anonymous ones (radar + blank-callsign
> ADS-B), rather than a fully time-ordered merge of both streams.
> **Alternatives considered:** merge all new ticks globally by
> `observed_at` and process one at a time.
> **Why:** keeps the Hungarian association a real batch operation each
> cycle instead of degrading to one-at-a-time greedy assignment. Traded
> away: a large `--since-id` backfill processed in a single pass can let
> multiple historical radar contacts from the same aircraft compete for
> one already-collapsed track. Measured: ~26% radar match rate on a
> 66-tick single-batch backfill vs. ~74% replaying the same data live in
> small poll cycles. Not a concern for normal live operation; noted as a
> known limitation in `docs/FUSION.md` rather than fixed, since it only
> bites bulk backfills.

This doc is what gets read in an interview — keep it honest, including the
tradeoffs that were made for time rather than "the right" reason.
