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

`./run.sh harness sweep <went_dark|kinematic> <start> <end>`: one injected
event per eligible aircraft (each its own run), the real fusion engine and
detectors, thresholds scaled to the window's measured ADS-B interval.

- **Recall** -- injected event flagged on the right aircraft within the
  latency budget: `adsb_silence_s` + one update interval for went-dark
  (docs/PROBLEM.md's target), `(consecutive + 1)` intervals for kinematic.
- **False-positive rate** -- share of aircraft that raise a false alarm
  on the *un-injected* window (ordinary traffic), each alert counted once.
  docs/PROBLEM.md's target: under 5%.
- **Induced** -- extra alerts in injected runs that weren't in the
  background, per run: side effects of the injection itself.

### Current (2026-10-13)

| Window | Interval | Detector | Budget | Recall | Mean latency | False-positive rate | Induced / run |
|---|---|---|---|---|---|---|---|
| NYC 2026-10-10 02:38-03:08 UTC | 11s | went-dark | 41s | **86.6%** (97/112) | 25.5s | **0.0%** (0/150) | 1.70 |
| Gulf 2026-10-10 02:01-02:16 UTC | 45s | went-dark | 112s | **94.2%** (114/121) | 38.2s | **0.8%** (1/122) | 0.50 |
| NYC, same window | 11s | kinematic | 33s | 32.9% (48/146) | 24.1s | 4.7% (7/150) | 2.25 |
| Gulf, same window | 45s | kinematic | 135s | 59.2% (71/120) | 92.0s | 0.8% (1/122) | 0.17 |

**Went-dark meets docs/PROBLEM.md's target on both feeds.** Went-dark
skips aircraft on the ground (38 NYC targets were taxiing at the
injected dropout and aren't scored). Kinematic has no stated target.

### How it got here

Each row changes one thing from the row above.

| Change | NYC went-dark recall / FP | Gulf went-dark recall / FP |
|---|---|---|
| Original: 15s budget, fixed 30s/10s thresholds, 800m gate, n*(n-1) FP metric | 6.0% / 24.5% | 1.6% / 0.0% |
| Thresholds scaled to ADS-B interval | -- | 31.1% (90s budget) / 0.85% |
| + covariance gate in `fusion/associate.py` | -- | 93.4% (90s) / 2.07% |
| + target = silence + 1 interval; FP counted once on clean traffic | 78.0% / 13.3% | 93.4% / 0.8% |
| + went-dark ignores ground traffic | **86.6% / 0.0%** | **94.2% / 0.8%** |

Kinematic: 1.4% -> 32.9% (NYC), 0% -> 59.2% (Gulf) after replacing the
injected 400 m/tick sideways ramp (a constant velocity the filter just
learned -- zero alerts on the target in traced runs) with a time-based
2g lateral acceleration.

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

### Known gaps

- Injected kinematic maneuvers only move the target's ADS-B positions --
  its radar contacts stay on the original path (radar ticks carry no
  link back to their source aircraft), so the target's track loses its
  radar and orphaned contacts start new tracks. That's most of the
  "induced" column; a real maneuver moves both.
- Kinematic recall is modest at NYC cadence: with `consecutive=2` and
  `sigma_mult=4`, a 2g turn is often absorbed before two consecutive
  updates exceed 4 sigma. No target exists for it yet.
- One 30-min NYC window and one 15-min Gulf window; neither is a
  held-out set separate from the windows the fixes were diagnosed on.
