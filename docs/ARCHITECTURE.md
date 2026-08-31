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

This doc is what gets read in an interview — keep it honest, including the
tradeoffs that were made for time rather than "the right" reason.
