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

Harness sweeps (`./run.sh harness sweep <kind> <start> <end>`): one
injected event per eligible aircraft, the real fusion engine and
detectors, thresholds scaled to the window's measured ADS-B interval.
"False positives" counts every alert that isn't the injected event,
across all runs, over n x (n-1) opportunities -- so background alerts on
real traffic recur once per run.

| Window | ADS-B interval | Detector | Recall (<=15s) | Recall (<=90s) | Mean latency | False positives |
|---|---|---|---|---|---|---|
| NYC, 2026-10-10 02:38-03:08 UTC, 150 aircraft | 11s | went-dark | **6.0%** | -- | 11.4s | **24.5%** |
| NYC, same window, 146 aircraft | 11s | kinematic | **1.4%** | -- | 3.5s | **23.4%** |
| Gulf, 2026-10-10 02:01-02:16 UTC, 122 aircraft, original fixed thresholds | 45s | went-dark | 1.6% | -- | 8.5s | 0.0% |
| Gulf, thresholds scaled to interval | 45s | went-dark | 0% | 31.1% | 41.9s | 0.85% |
| Gulf, scaled + covariance gate (`fusion/associate.py`) | 45s | went-dark | 0% | **93.4%** | 38.2s | **2.07%** |
| Gulf, original thresholds | 45s | kinematic | 0% | -- | -- | 0.0% |

**Neither docs/PROBLEM.md target is met yet.** What the numbers say:

- **Went-dark within 15s is structurally out of reach at 30s silence.**
  The detector can't fire until ADS-B has been silent `adsb_silence_s`
  (30s, raised from 10s because real OpenSky poll jitter swamped it), so
  a detection within 15s only happens when the last pre-dropout fix was
  already >15s old. The 15s target and the 30s jitter threshold
  contradict each other; one of them has to move.
- **NYC false positives are background alerts, not injection
  side-effects.** With nothing injected, the same 30 min of NYC traffic
  produces ~7 kinematic alerts (all on ordinary ADS-B tracks); the sweep
  re-counts them in every one of its 146 runs. Went-dark is the same
  pattern: real OpenSky gaps longer than 30s while radar (derived from
  the last fix) is still recent.
- **The kinematic injection is absorbed, not detected.** In traced runs,
  zero alerts land on the injected target: `inject_kinematic_jump`'s
  400 m/tick ramp is a constant ~40 m/s sideways velocity, which a
  constant-velocity Kalman filter with `accel_std=3` simply tracks as a
  new velocity after a couple of updates. A step change (or a ramp that
  accelerates) would be what the residual test can actually see.
- **At 45s cadence, gating -- not detection -- was the bottleneck.**
  Once ADS-B drops, the next radar contact is checked against a 45s
  prediction (measured error: median 954m, p95 3.6km); the fixed 800m
  gate rejected most of them, so the dark track never got the radar
  corroboration went-dark needs. The covariance gate took recall from
  31% to 93% with false positives still under 5%.
