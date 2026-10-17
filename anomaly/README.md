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

### Current (2026-10-15)

Three kinds of window:

- **validation** -- never looked at until the code was frozen: two Gulf
  windows from other days, and a fresh NYC capture made for this
  (2026-10-15, its radar carries exact ground truth).
- **held-out** -- captured after commit 6fa89bb froze the thresholds;
  one of them (Oct 14) was then used to diagnose the misses below, so
  it's development data now.
- **tuning** -- what the earlier fixes were developed on.

| Window | Role | Interval | Recall | Mean latency | False-positive rate | Induced / run |
|---|---|---|---|---|---|---|
| NYC 2026-10-15 22:50-23:20 UTC | validation | 11s | **91.1%** (205/225) | 25.3s | **0.0%** (0/282) | 2.37 |
| Gulf 2026-10-12 05:00-05:15 UTC | validation | 45s | **89.9%** (107/119) | 48.6s | **0.0%** (0/121) | 0.97 |
| Gulf 2026-10-11 05:45-06:00 UTC | validation | 45s | **94.2%** (131/139) | 45.4s | **1.4%** (2/140) | 0.91 |
| Gulf 2026-10-13 05:30-05:45 UTC | held-out | 45s | 93.8% (122/130) | 41.9s | 0.0% (0/132) | 0.66 |
| Gulf 2026-10-14 06:15-06:30 UTC | held-out, then dev | 45s | 94.7% (124/131) | 49.5s | 0.7% (1/134) | 0.55 |
| Gulf 2026-10-10 02:01-02:16 UTC | tuning | 45s | 98.4% (122/124) | 44.3s | 0.8% (1/125) | 0.80 |
| NYC 2026-10-10 02:38-03:08 UTC | tuning | 11s | 88.3% (98/111) | 25.6s | 0.0% (0/150) | 1.97 |

Went-dark only; budget is silence + one interval (41s NYC, 112s Gulf).
**On data nothing was tuned on: 89.9-94.2% recall, 0.0-1.4% false
alarms -- docs/PROBLEM.md's < 5% target met on all three.**

Kinematic (secondary signal, no target):

| Window | Role | Recall | False-positive rate | Induced / run |
|---|---|---|---|---|
| NYC 2026-10-15 | validation | 49.5% (137/277) | 1.1% (3/282) | 2.86 |
| Gulf 2026-10-12 | validation | 49.6% (59/119) | 0.8% (1/121) | 0.48 |
| Gulf 2026-10-11 | validation | 17.5% (24/137) | 0.0% (0/140) | 0.54 |
| Gulf 2026-10-13 | held-out | 24.4% (31/127) | 2.3% (3/132) | 0.69 |
| Gulf 2026-10-14 | held-out, then dev | 26.0% (34/131) | 3.0% (4/134) | 0.52 |
| Gulf 2026-10-10 | tuning | 25.6% (31/121) | 1.6% (2/125) | 0.56 |
| NYC 2026-10-10 | tuning | 44.5% (65/146) | 4.0% (6/150) | 2.58 |

It swings 17-50% between windows and stays under 5% false alarms, so
it's worth surfacing when it fires and not something to claim coverage
on. The drop from the previous 59% is the injection getting more
realistic (the target's radar now moves too), not the detector getting
worse -- see below.

### What fixed the held-out misses

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
| + likelihood assignment cost, 2.5x staleness gate | **88.3% / 0.0%** | **98.4% / 0.8%** |

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
- **Kinematic is insensitive at 45s cadence.** The filter's own
  prediction spread over 45s is ~3 km, so a 2g deviation needs most of
  an interval to clear 4 sigma, and the filter partly absorbs it after
  the first update.
