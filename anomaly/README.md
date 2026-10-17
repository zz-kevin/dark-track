# anomaly/

Consumes the fused picture from `fusion/` and flags aircraft behaving like
they're trying not to be seen (weeks 10–11).

- **Went-dark detector** — the cooperative (ID'd) source drops while the
  non-cooperative contact keeps tracking the same kinematic profile.
- **Kinematic-inconsistency detector** — a speed/heading change outside what
  the vehicle type should plausibly do.
- **Test harness** — injects synthetic anomalies into a replay run so
  detectors can be scored against a known-answer set.

The point of this module isn't "it detects anomalies" — it's a measured
precision/recall table against the injected test set. Record real numbers
here, not just working code.

## Measured results

`./run.sh harness sweep <went_dark|kinematic> <start> <end> --region <nyc|gulf>`:
one injected event per eligible aircraft (each its own run), the real
fusion engine and detectors, thresholds scaled to the window's measured
ADS-B interval.

- **Recall** -- injected event flagged on the right aircraft within the
  latency budget: `adsb_silence_s` + one update interval for went-dark
  (docs/PROBLEM.md's target), `(consecutive + 1)` intervals for kinematic.
- **False-positive rate** -- share of aircraft that raise a false alarm
  on the *un-injected* window (ordinary traffic), each alert counted once.
  docs/PROBLEM.md's target: under 5%.
- **Induced** -- extra alerts in injected runs that weren't in the
  background, per run: side effects of the injection itself.

### Current (2026-10-16)

Window roles:

- **validation** -- scored once, after the code was frozen, and never
  used to choose anything: Gulf 2026-10-13 20:00, Gulf 2026-10-12 20:30,
  and a second fresh NYC capture (2026-10-16 01:30). These are the
  numbers to quote.
- **validation, earlier round** -- the previous change's validation
  windows (NYC 2026-10-15, Gulf 2026-10-12 05:00, Gulf 2026-10-11).
  Not used to choose anything in this round either, but seen before.
- **development** -- used to diagnose and choose: Gulf 2026-10-14,
  Gulf 2026-10-13 05:30, and the original tuning windows (Gulf and NYC
  2026-10-10).

**Went-dark** (budget = silence + one interval: 41s NYC, 112s Gulf):

| Window | Role | Interval | Recall | Mean latency | False-positive rate | Induced / run |
|---|---|---|---|---|---|---|
| NYC 2026-10-16 01:30-02:00 UTC | validation | 11s | **90.0%** (135/150) | 26.6s | **0.0%** (0/189) | 2.32 |
| Gulf 2026-10-13 20:00-20:15 UTC | validation | 45s | **93.5%** (101/108) | 45.9s | **0.0%** (0/108) | 1.10 |
| Gulf 2026-10-12 20:30-20:45 UTC | validation | 45s | **96.5%** (111/115) | 50.3s | **0.0%** (0/115) | 0.94 |
| NYC 2026-10-15 22:50-23:20 UTC | validation, earlier | 11s | 91.6% (206/225) | 25.3s | 0.0% (0/282) | 2.38 |
| Gulf 2026-10-12 05:00-05:15 UTC | validation, earlier | 45s | 90.8% (108/119) | 48.7s | 0.0% (0/121) | 1.01 |
| Gulf 2026-10-11 05:45-06:00 UTC | validation, earlier | 45s | 94.2% (131/139) | 45.4s | 0.0% (0/140) | 0.88 |
| Gulf 2026-10-13 05:30-05:45 UTC | development | 45s | 94.6% (123/130) | 42.4s | 0.0% (0/132) | 0.65 |
| Gulf 2026-10-14 06:15-06:30 UTC | development | 45s | 95.4% (125/131) | 49.5s | 0.0% (0/134) | 0.52 |
| Gulf 2026-10-10 02:01-02:16 UTC | development | 45s | 97.6% (121/124) | 44.5s | 0.0% (0/125) | 0.78 |
| NYC 2026-10-10 02:38-03:08 UTC | development | 11s | 90.1% (100/111) | 25.7s | 0.0% (0/150) | 2.00 |

**90-96.5% recall on the validation windows, and no false went-dark
alarm on ordinary traffic in any of the ten** (docs/PROBLEM.md target:
under 5%).

**Kinematic** (budget = 3 update intervals; maneuver direction seeded
per aircraft, so these reproduce exactly):

| Window | Role | Recall | False-positive rate | Induced / run |
|---|---|---|---|---|
| NYC 2026-10-16 | validation | 35.6% (67/188) | 1.1% (2/189) | 2.88 |
| Gulf 2026-10-13 20:00 | validation | 78.2% (79/101) | 0.0% (0/108) | 0.24 |
| Gulf 2026-10-12 20:30 | validation | 91.1% (102/112) | 2.6% (3/115) | 0.12 |
| NYC 2026-10-15 | validation, earlier | 43.0% (119/277) | 0.7% (2/282) | 2.90 |
| Gulf 2026-10-12 05:00 | validation, earlier | 86.6% (103/119) | 0.8% (1/121) | 0.15 |
| Gulf 2026-10-11 | validation, earlier | 81.0% (111/137) | 0.0% (0/140) | 0.16 |
| Gulf 2026-10-13 05:30 | development | 85.0% (108/127) | 0.8% (1/132) | 0.15 |
| Gulf 2026-10-14 | development | 82.4% (108/131) | 0.7% (1/134) | 0.14 |
| Gulf 2026-10-10 | development | 77.7% (94/121) | 0.0% (0/125) | 0.22 |
| NYC 2026-10-10 | development | 34.9% (51/146) | 1.3% (2/150) | 2.70 |

Kinematic went from a weak secondary signal (15-50%) to 78-91% on Gulf
once fusion measures ADS-B velocity: a 2g maneuver now shows up as a
velocity the filter believes, instead of a position it partly writes
off as noise. NYC stays at 35-43% -- with 11s updates the deviation
has to clear 4 sigma within 33s, and approach traffic turns all the
time. Still no stated target; false alarms under 3% everywhere.

### What changed this round

ADS-B speed/track became a filter measurement (airborne fixes only),
and ADS-B position noise went 20 m -> 50 m to absorb OpenSky timestamp
jitter (docs/FUSION.md "State estimation"). Chosen from a 5-config grid
on the development windows, frozen, then scored against the previous
config on the three validation windows:

| Validation window | Went-dark | Went-dark FP | Kinematic | Kinematic FP |
|---|---|---|---|---|
| NYC 2026-10-16 | 89.3% -> 90.0% | 0.0% -> 0.0% | 40.4% -> 36.2% | 3.7% -> 1.1% |
| Gulf 2026-10-13 20:00 | 90.7% -> 93.5% | 2.8% -> 0.0% | 14.9% -> 73.3% | 1.9% -> 0.0% |
| Gulf 2026-10-12 20:30 | 95.7% -> 96.5% | 0.0% -> 0.0% | 25.9% -> 92.9% | 4.3% -> 2.6% |

(That comparison ran before the maneuver direction was seeded, so its
kinematic figures differ by a few points from the table above.)

### What fixed the held-out misses (previous round)

On the Oct 14 window the committed code scored 84.0% (89.2% on Oct 13).
Of its 21 misses, 20 had radar contacts for minutes after the dropout
that never stayed on the dark track. Tracing them found two mechanisms:

1. **Distance-only assignment.** At 45s cadence the dark track's
   prediction is ~1 km off but honestly uncertain (~4 km std); a
   neighbour updated seconds ago, 600 m away with a tight covariance,
   won the contact on distance. Fix: likelihood cost (Mahalanobis^2 +
   log det S) in fusion/associate.py. Alone: 84.0% -> 84.7%.
2. **The staleness gate.** One dropped radar contact (radar_sim drops
   10%) put the next one 94s after the track's last update, past the 90s
   (2x interval) gate -- the dark track was never a candidate again.
   Fix: 2.5x interval. With both: 84.0% -> 94.7%. Gating for the whole
   track TTL got 96.9% on Gulf but took NYC false alarms 0% -> 4%.

That config was then frozen and scored on the validation windows above.

The NYC tuning rows are re-runs with the current code. That window
turned out to hold 20 Gulf ticks too (the Gulf poller came back for its
last 5 minutes), fused on the same plane as NYC -- negligible here, but
other windows from that night mix the regions heavily, so every replay
tool now filters to one region (`--region`, ingestion/regions.py).

### How it got here

Each row changes one thing from the row above (went-dark, tuning windows).

| Change | NYC went-dark recall / FP | Gulf went-dark recall / FP |
|---|---|---|
| Original: 15s budget, fixed 30s/10s thresholds, 800m gate, n*(n-1) FP metric | 6.0% / 24.5% | 1.6% / 0.0% |
| Thresholds scaled to ADS-B interval | -- | 31.1% (90s budget) / 0.85% |
| + covariance gate in `fusion/associate.py` | -- | 93.4% (90s) / 2.07% |
| + target = silence + 1 interval; FP counted once on clean traffic | 78.0% / 13.3% | 93.4% / 0.8% |
| + went-dark ignores ground traffic | 86.6% / 0.0% | 94.2% / 0.8% |
| + one region per replay, time-sliced association (re-measured) | 87.4% / 0.0% | 91.9% / 0.8% |
| + likelihood assignment cost, 2.5x staleness gate | 88.3% / 0.0% | 98.4% / 0.8% |
| + ADS-B velocity measured, 50 m ADS-B position noise | **90.1% / 0.0%** | **97.6% / 0.0%** |

Kinematic, Gulf tuning window: 0% -> 59.2% after replacing the injected
400 m/tick sideways ramp (a constant velocity the filter just learned)
with a time-based 2g lateral acceleration; -> 28.1% once the target's
radar contacts are moved by the same maneuver. Before that, only ADS-B
moved: the target's two sensors disagreed by kilometres, which is easy
to flag and not what a real maneuver looks like.

### What each fix was for

- **15s was unreachable by construction.** Went-dark can't fire before
  ADS-B has been silent `adsb_silence_s` (30s, raised from 10s because
  OpenSky poll jitter swamped it). The target is now silence + one
  interval -- see docs/PROBLEM.md.
- **At 45s cadence, gating was the bottleneck.** After a dropout the next
  radar contact is checked against a 45s prediction (measured error:
  median 954m, p95 3.6km); the fixed 800m gate rejected most of them, so
  the dark track never got the radar corroboration went-dark needs.
- **All 27 NYC false went-dark alerts were ground traffic** (26 with no
  barometric altitude, which is how OpenSky reports on-ground, 1 at
  -84m): patchy airport ADS-B, and taxiing neighbours' radar contacts
  within the gate kept the silent track "corroborated".
- **The old FP metric re-counted the same real-traffic alerts in every
  run** (n runs x background alerts over n*(n-1)) and scored other
  detectors' alerts as false positives.
- **Injected maneuvers move both sensors** (`inject.py`, using
  ingestion/radar_truth.py to find the target's radar contacts).

### Known gaps

- **One validation capture per region-night.** Three validation windows
  is enough to see the went-dark numbers hold, not to put tight error
  bars on them. Every new capture is free validation data -- score it
  before changing anything.
- **Induced alerts are still high at NYC** (2.05 / run went-dark, 2.65
  kinematic): mostly the target's own contacts or neighbours being
  re-associated once the target's picture changes. Not counted in the
  false-positive rate, which is measured on clean traffic.
- **The injected 2g maneuver is a sustained push in one direction** --
  displacement grows as t^2 without bound, unlike a real turn. Fine as a
  "can the detector see it" test; not a model of real flight.
- **Kinematic at NYC cadence stays at 35-43%** -- see above.
