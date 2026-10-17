# Architecture

_Status: data flow proven end to end, from OpenSky to the web UI. Keep the
diagram to one page; the decisions log below carries the reasoning._

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
- **api/** — thin read-only layer serving fused tracks, track history,
  the anomaly feed, and replays (fusion + detection re-run in memory over
  a stored window, optionally with an injected anomaly) to the frontend.
  Not the interesting part of this project — keep it simple.
- **web/** — Leaflet map: live picture, replay player with an alert
  timeline, click-to-inspect track history.

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
> **Amended:** the contact's position is now dead-reckoned forward along
> the ADS-B speed/heading by the same latency, so position and timestamp
> agree. Originally the position stayed at the ADS-B fix -- invisible on
> slow NYC approach traffic, but on Gulf cruise traffic (~227 m/s median)
> it put contacts up to ~1km behind their own track, past the 800m gate.
> Measured on the same 15 min of Gulf data: 82.4% -> 98.4% of radar
> contacts associated.

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

> **Decision:** Identify watched military aircraft types (F-15E, F-35,
> A-10, F-16, EA-18G, B-2, HC-130J, HH-60W/UH-60, E-3, E-2D, P-8A,
> F/A-18E/F, MQ-9, MQ-4C) by
> joining ADS-B `icao24` against OpenSky's aircraft metadata CSV, loaded
> once at poller startup and filtered down to watched airframes only.
> **Alternatives considered:** per-aircraft calls to OpenSky's metadata
> endpoint; callsign-prefix heuristics.
> **Why:** the CSV is a one-time download with no extra API calls against
> the anonymous rate limit, and filtering at load keeps memory to a few
> hundred entries instead of ~500k rows. Callsigns for these aircraft are
> mission-assigned and change, so they're not a stable identity. Type is
> stored on `raw_ticks` and inherited by `fused_tracks`, so a track keeps
> its type after ADS-B goes dark and only radar is still seeing it.
> RQ-180 and LUCAS are in the list but marked untrackable: neither has a
> transponder, so no ADS-B-based method can identify them. Carriers
> aren't tracked either (no ADS-B, AIS usually dark), so they're
> reference data only. Both would need a different sensor model, not more
> matching rules.

> **Decision:** Went-dark is scored against a cadence-scaled target
> (silence threshold + one ADS-B interval), false positives as the share
> of aircraft alarming on *un-injected* traffic, and only on airborne
> aircraft; final numbers come from windows captured after the
> thresholds were frozen.
> **Alternatives considered:** keep the original 15s target; keep the
> n*(n-1) false-positive metric; quote the tuning-window numbers.
> **Why:** 15s is unreachable with a 30s silence threshold (forced by
> OpenSky poll jitter); the old FP metric re-counted the same alerts once
> per run; all 27 NYC false alarms were taxiing traffic. Held-out recall
> came in 3-8 points below the tuning window (anomaly/README.md) -- the
> number to quote.

> **Decision:** Ground truth for synthetic radar contacts lives in
> `raw_ticks.truth_tick_id` (written by radar_sim, never read by
> fusion), with `ingestion/radar_truth.py` inferring it for older
> contacts.
> **Alternatives considered:** score fusion only through ADS-B
> callsigns; a separate truth table.
> **Why:** fusion accuracy can't be measured without knowing which
> aircraft each anonymous contact came from, and injected maneuvers have
> to move the target's radar too. Inference (latency window + identical
> altitude + speed/heading jitter + position) labels 99.2% of NYC and
> 100% of Gulf contacts unambiguously; the rest are left unscored rather
> than guessed. Same table, nullable column: nothing to join or keep in
> sync.

> **Decision:** Fusion polls Postgres every 0.25s and writes each cycle
> in one transaction.
> **Alternatives considered:** LISTEN/NOTIFY from the producers; keep
> the 3s poll.
> **Why:** the 500 ms tick -> fused-output target is mostly poll wait:
> at 3s, 27% of ticks could make it regardless of compute. Two indexed
> SELECTs every 0.25s are cheap and need no producer changes;
> LISTEN/NOTIFY would cut the wait further but touches every writer.
> One connection per touched track was the other half (p95 759 ms per
> cycle on its own). Measured after: 100% of ticks under 500 ms at ~520
> concurrent tracks (fusion/bench.py).

> **Decision:** One region per fusion run (`--region`); replay tools
> refuse a mixed-region window.
> **Alternatives considered:** a proper map projection, or per-track
> local frames, so one process can fuse anywhere.
> **Why:** a single flat-earth plane is fine at regional scale and keeps
> the filter math trivial. Two pollers once ran at the same time
> (2026-10-10 ~02:30-03:10 UTC), and a window from then fused as one
> puts the reference point between New York and Qatar -- roughly a 10%
> east-west distance error in each region. Filtering by region is a few
> lines; a global projection would be a rewrite for a case that only
> comes up when two regions are captured at once.

> **Decision:** Association cost is the negative log-likelihood under
> each track's predicted covariance (Mahalanobis^2 + log det S), and the
> staleness gate is 2.5 ADS-B intervals (was Euclidean distance, 2x).
> **Alternatives considered:** keep Euclidean cost; gate for the track's
> whole TTL; feed ADS-B velocity into the filter to shrink the 45s
> prediction error instead.
> **Why:** tracing held-out went-dark misses found two mechanisms --
> a confident neighbour beating the dark track's uncertain prediction on
> distance, and one dropped radar contact pushing the next one past the
> 2x gate. Developed on the Oct 14 window; frozen, then scored on two
> Gulf windows nothing had touched (89.9% / 94.2% recall, 0.0% / 1.4%
> false alarms) and a fresh NYC capture (91.1%, 0.0%). Gating for the whole TTL
> recovered more Gulf recall (96.9%) but took NYC false alarms 0% -> 4%,
> so it stopped at 2.5x. The velocity measurement is still worth doing,
> but it changes the filter, not just association, and needs its own
> evaluation.

> **Decision:** ADS-B velocity is a filter measurement on airborne
> updates (10 m/s), and ADS-B position noise is 50 m (was position-only,
> 20 m).
> **Alternatives considered:** position noise 50 m alone; velocity at
> 5 m/s with 20 m; velocity on ground fixes too.
> **Why:** a 5-config grid on two development windows. Velocity was a
> large win on Gulf (accuracy 94.7% -> 99.3%, kinematic recall 27% ->
> 85%) and hurt NYC at 20 m position noise, because OpenSky's position
> timestamps jitter and pinned velocity left nowhere to absorb it.
> 50 m + velocity, skipping taxiing aircraft, was the best overall; the
> 50 m-only setting was better on NYC all-traffic accuracy alone. Frozen,
> then scored against the previous config on three untouched windows,
> including a second fresh NYC capture: better or equal on every metric
> except NYC kinematic recall (40% -> 36%, false alarms 3.7% -> 1.1%).

> **Decision:** Keep the constant-velocity filter; the IMM filter
> (`fusion/imm.py`) stays as a tested, documented opt-in.
> **Alternatives considered:** IMM (constant velocity + coordinated
> turn) as the default, aimed at transponder dropouts during turns.
> **Why:** on the window it was built for, went-dark recall didn't move
> (79.6% -> 78.6%) and kinematic recall fell from 78% to 1-4%, because
> a filter that explains maneuvers stops flagging them. The traced
> misses are holding patterns sampled every 45 s, which no motion model
> predicts; altitude would separate the stack, but needs a realistic
> radar height error in the simulator first. Kept in the repo because
> the negative result is the finding, and the mixture-likelihood
> association it needed is exact for one model.

> **Decision:** Simulated radar measures height (300 m error); altitude
> in association is a tested opt-in, off by default. Went-dark scoring
> only counts dropouts the target's own radar could reveal.
> **Alternatives considered:** altitude on by default; leave radar
> heights exact; keep scoring every injected dropout.
> **Why:** exact radar heights were never realistic. Altitude fixed the
> traced holding-stack miss, but was neutral on six validation windows
> and slightly negative in same-level formations, so it doesn't earn the
> default (same rule as the velocity time offset). The scorer fix is a
> correction, not a tuning: dropouts injected into real coverage gaps
> -- ADS-B and the radar derived from it already silent for minutes --
> were counted as misses no detector could avoid; the old scoring stays
> available as `--include-coverage-gaps`.

> **Decision:** One alert per anomaly: went-dark episodes stay open
> until ADS-B resumes or the track retires; kinematic episodes are per
> track and close after as many normal updates as it took to open.
> **Alternatives considered:** the stateless "flagged this cycle"
> episodes the detectors had; de-duplicating in the UI only.
> **Why:** walking through the demo, one injected dropout produced three
> went-dark alerts minutes apart -- the episode closed on every routine
> radar gap. An operator would see the same aircraft re-alerted, and the
> live engine wrote duplicate episodes. The harness never showed it
> because it scores the first detection; the evidence was in the
> "induced alerts" column, which had been misattributed to neighbouring
> tracks. Fixed in the shared detector so live, replay and scoring agree.

> **Decision:** `api/` is a stdlib `http.server`, read-only, serving
> `web/` from the same origin; replays re-run fusion + detection in
> memory per request (cached).
> **Alternatives considered:** FastAPI/Flask; precomputing replays into
> tables.
> **Why:** the API is deliberately not the interesting part -- no new
> dependency, no CORS. Re-running a 15-minute window takes ~1s, and
> it means a replay always reflects current fusion code and can inject
> the same anomalies the harness scores, which a stored replay couldn't.

This doc is what gets read in an interview — keep it honest, including the
tradeoffs that were made for time rather than "the right" reason.
