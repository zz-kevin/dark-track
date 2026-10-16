# Problem statement

## What this is

Dark Track fuses two independent, noisy sensor feeds — real ADS-B aircraft
data (cooperative, carries an ID) and a simulated non-cooperative radar
contact (noisy, latent, no ID) — into a single tracked picture of the
airspace over a region I choose. It then flags aircraft that behave like
they're trying not to be seen: a transponder that goes quiet while the
radar-like contact keeps flying the same kinematic profile.

This mirrors, at small scale, the actual problem real air-surveillance and
intelligence-fusion systems solve: combining cooperative and non-cooperative
sensor data into one confident picture, and noticing when something doesn't
add up.

## Why two sources, not one

A single ADS-B feed is just a flight tracker — there's no track-association
or entity-resolution problem to solve. The second, synthetic source (position
noise + latency jitter + random dropout + no ID field) is what forces real
gating, association, and conflict-resolution logic. See `ingestion/` for how
it's generated from real truth data.

## Success criteria

Status as of 2026-10-15 — measured, with the commands to reproduce in
each module's README.

| Criterion | Target | Measured | |
|---|---|---|---|
| Fusion accuracy | 90% of aircraft | Gulf 95.1–97.5%; NYC 87.8% (97.4% airborne) — validation windows | met on Gulf; NYC **just short**, all of it ground traffic |
| Went-dark false alarms | < 5% on held-out | 0.0–1.4% (3 validation windows) | met |
| Went-dark detection | within silence + 1 interval | 89.9–94.2% of injected events (validation) | see anomaly/README.md |
| Throughput | 150 tracks, < 500 ms | 100% of ticks < 500 ms at ~510 concurrent tracks (max 310 ms) | met |

"Validation" = windows nothing was tuned or diagnosed on: two Gulf
captures from other days and a fresh NYC capture (2026-10-15).

- **Fusion accuracy:** correctly associate the same real-world aircraft
  across both sources within `300` meters and `5` seconds, for `90`% of
  tracks in a test window.

  _Measured_ by `fusion/accuracy.py` against ground truth recovered for
  every synthetic radar contact (`ingestion/radar_truth.py`): a contact
  is correct if it updated its own aircraft's track, the fused position
  right after is within 300 m of the aircraft's true position, and it was
  fused within 5 s; an aircraft counts if at least 90% of its contacts
  are correct. Gulf passes (95.1–97.5% on validation windows). NYC comes
  in at 87.8% on its validation capture: airborne aircraft pass at 97.4%,
  and the shortfall is ground traffic swapping contacts between taxiing
  neighbours. See docs/FUSION.md.
- **Anomaly detection:** flag an injected "went dark" event within the
  ADS-B silence threshold plus one update interval of it occurring
  (~`41` s at 10s polling), with under `5`% of aircraft raising a false
  alarm on a held-out replay set.

  _Revised from "within 15 seconds":_ the went-dark detector can't fire
  until ADS-B has been silent `adsb_silence_s`, which had to be raised
  from 10s to 30s because real OpenSky poll jitter produced constant
  false alarms at 10s. A 15s target with a 30s silence threshold is
  unreachable by construction (measured: 6% recall). The target now
  scales with the feed's cadence instead of assuming one.

  _Measured on validation data:_ 0.0–1.4% false-positive rate, 89.9–94.2%
  of injected events flagged within the budget (two Gulf windows and a
  fresh NYC capture, none used for tuning).
- **Throughput:** sustain fusion + anomaly detection for at least `150`
  concurrent tracks with end-to-end (sensor tick → fused output) latency
  under `500` ms.

  _Measured_ by `fusion/bench.py` (real fusion, real Postgres writes, both
  detectors, a captured window overlaid up to 4x for more concurrent
  traffic): 100% of ticks reach `fused_tracks` in under 500 ms at ~510
  concurrent Gulf tracks (p95 250 ms, max 310 ms). That needed three
  changes: the fusion poll interval (3s -> 0.25s; at 3s only 27% of ticks
  could make it), one transaction per cycle instead of one connection per
  track, and vectorized gating.

## Non-goals

- Not building a production-grade distributed system. A single process is a
  legitimate architecture for the whole semester unless there's a specific
  reason to split it.
- Not integrating classified, proprietary, or access-controlled data of any
  kind. Everything here runs on public APIs and self-generated synthetic
  data.
- Not chasing 100% detection accuracy — a measured, honest precision/recall
  number beats an unverified claim of "it works."
