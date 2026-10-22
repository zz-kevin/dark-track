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

### Current (2026-10-16, final)

Window roles:

- **validation** -- scored once, after the code was frozen, never used
  to choose anything: Gulf 2026-10-12 04:00, 2026-10-13 02:00,
  2026-10-14 04:30, and a third fresh NYC capture's second half
  (2026-10-16 18:08, daytime). These are the numbers to quote.
- **validation, earlier** -- earlier rounds' validation windows,
  including both fresh NYC captures. Not used to choose anything since,
  but seen before.
- **development** -- used to diagnose and choose: the Gulf morning
  capture's 04:10 and 04:45 windows (where the IMM, altitude and the
  FRS91 altitude glitch were investigated), the NYC daytime capture's
  first half (2026-10-16 17:52, the velocity time-offset test), Gulf 2026-10-14 06:15,
  2026-10-13 05:30, and the original tuning windows (2026-10-10).

**Scoring change (2026-10-16):** a went-dark injection is only scored
if the target's *own* radar contacts fall where the detector could
fire within budget. The old check looked for any aircraft's radar
contact after the cutoff, so dropouts injected into real coverage gaps
-- ADS-B, and the radar derived from it, already silent for minutes --
counted as misses no detector could avoid (traced: UAE78P and UAE63Q,
Gulf 2026-10-16). The last column counts those, now skipped;
`--include-coverage-gaps` reproduces the old scoring (on the earlier
validation windows it gave 92.9-95.5% where this gives 95.5-97.1%).

**Went-dark** (budget = silence + one interval: 41s NYC, 112s Gulf):

| Window | Role | Interval | Recall | Mean latency | False-positive rate | Induced / run | Coverage gaps skipped |
|---|---|---|---|---|---|---|---|
| NYC 2026-10-16 18:08-18:23 UTC | validation | 11s | **98.3%** (119/121) | 27.1s | **0.0%** (0/165) | 0.01 | 2 |
| Gulf 2026-10-12 04:00-04:15 UTC | validation | 45s | **98.1%** (106/108) | 49.8s | **0.0%** (0/111) | 0.03 | 2 |
| Gulf 2026-10-13 02:00-02:15 UTC | validation | 46s | **93.3%** (97/104) | 51.4s | **0.0%** (0/106) | 0.03 | 0 |
| Gulf 2026-10-14 04:30-04:45 UTC | validation | 46s | **95.9%** (93/97) | 52.9s | **0.0%** (0/102) | 0.03 | 4 |
| Gulf 2026-10-11 23:30-23:45 UTC | validation, earlier | 46s | **95.5%** (105/110) | 46.4s | **0.0%** (0/113) | 0.09 | 3 |
| Gulf 2026-10-13 07:00-07:15 UTC | validation, earlier | 45s | **97.1%** (100/103) | 47.7s | **0.0%** (0/107) | 0.02 | 3 |
| Gulf 2026-10-14 08:00-08:15 UTC | validation, earlier | 46s | **95.5%** (105/110) | 50.6s | **0.0%** (0/111) | 0.06 | 1 |
| Gulf 2026-10-16 04:30-04:45 UTC | validation, earlier | 45s | **95.6%** (43/45) | 52.4s | **2.0%** (1/50) | 0.20 | 3 |
| NYC 2026-10-16 01:30-02:00 UTC | validation, earlier | 11s | **93.7%** (134/143) | 26.6s | **0.0%** (0/189) | 0.01 | 7 |
| Gulf 2026-10-13 20:00-20:15 UTC | validation, earlier | 45s | **99.0%** (101/102) | 45.9s | **0.0%** (0/108) | 0.10 | 6 |
| Gulf 2026-10-12 20:30-20:45 UTC | validation, earlier | 46s | **97.4%** (111/114) | 50.3s | **0.0%** (0/115) | 0.00 | 1 |
| NYC 2026-10-15 22:50-23:20 UTC | validation, earlier | 11s | **94.9%** (203/214) | 25.5s | **0.0%** (0/282) | 0.00 | 11 |
| Gulf 2026-10-12 05:00-05:15 UTC | validation, earlier | 45s | **94.7%** (107/113) | 48.8s | **0.0%** (0/121) | 0.08 | 6 |
| Gulf 2026-10-11 05:45-06:00 UTC | validation, earlier | 46s | **96.3%** (131/136) | 45.4s | **0.0%** (0/140) | 0.14 | 3 |
| Gulf 2026-10-16 04:45-05:00 UTC | development | 46s | **88.4%** (84/95) | 52.2s | **1.0%** (1/105) | 0.03 | 5 |
| Gulf 2026-10-16 04:10-04:25 UTC | development | 46s | **83.5%** (76/91) | 48.3s | **0.0%** (0/105) | 0.07 | 7 |
| Gulf 2026-10-13 05:30-05:45 UTC | development | 45s | **97.6%** (123/126) | 42.4s | **0.0%** (0/132) | 0.01 | 4 |
| Gulf 2026-10-14 06:15-06:30 UTC | development | 45s | **96.9%** (125/129) | 49.5s | **0.0%** (0/134) | 0.02 | 2 |
| Gulf 2026-10-10 02:01-02:16 UTC | development | 45s | **97.6%** (121/124) | 44.5s | **0.0%** (0/125) | 0.02 | 0 |
| NYC 2026-10-16 17:52-18:07 UTC | development | 11s | **98.4%** (124/126) | 26.7s | **0.0%** (0/181) | 0.01 | 3 |
| NYC 2026-10-10 02:38-03:08 UTC | development | 11s | **96.0%** (95/99) | 25.8s | **0.0%** (0/150) | 0.01 | 12 |

The four validation rows, Gulf 2026-10-16 04:10 and NYC 2026-10-10
02:38 are re-scored with the 10 km gate cap added for the realistic
radar (below), which cost one aircraft on each of two validation
windows (Gulf 10-13 98 -> 97, 10-14 94 -> 93); the other rows predate it.

**93.3-98.3% recall on the validation windows, no false alarms on
ordinary traffic** (docs/PROBLEM.md target: under 5%; 0-2% across all
19 windows). The low end is the development windows from the Gulf
morning capture (85.7%, 88.4%): holding patterns sampled every 45 s,
and same-level formation pairs (FRS41/FRS42) -- docs/FUSION.md "Known
limitations".

**Kinematic** (budget = 3 update intervals; maneuver direction seeded
per aircraft, so these reproduce exactly):

| Window | Role | Recall | False-positive rate | Induced / run |
|---|---|---|---|---|
| NYC 2026-10-16 18:08-18:23 UTC | validation | 61.5% (99/161) | 0.0% (0/165) | 1.55 |
| Gulf 2026-10-12 04:00-04:15 UTC | validation | 89.7% (96/107) | 0.0% (0/111) | 0.10 |
| Gulf 2026-10-13 02:00-02:15 UTC | validation | 89.5% (94/105) | 0.0% (0/106) | 0.10 |
| Gulf 2026-10-14 04:30-04:45 UTC | validation | 83.0% (83/100) | 0.0% (0/102) | 0.19 |
| Gulf 2026-10-11 23:30-23:45 UTC | validation, earlier | 87.3% (96/110) | 0.9% (1/113) | 0.12 |
| Gulf 2026-10-13 07:00-07:15 UTC | validation, earlier | 88.8% (95/107) | 0.0% (0/107) | 0.11 |
| Gulf 2026-10-14 08:00-08:15 UTC | validation, earlier | 90.0% (99/110) | 0.0% (0/111) | 0.15 |
| Gulf 2026-10-16 04:30-04:45 UTC | validation, earlier | 76.0% (38/50) | 0.0% (0/50) | 0.16 |
| NYC 2026-10-16 01:30-02:00 UTC | validation, earlier | 35.6% (67/188) | 1.1% (2/189) | 2.80 |
| Gulf 2026-10-13 20:00-20:15 UTC | validation, earlier | 78.2% (79/101) | 0.0% (0/108) | 0.24 |
| Gulf 2026-10-12 20:30-20:45 UTC | validation, earlier | 91.1% (102/112) | 2.6% (3/115) | 0.12 |
| NYC 2026-10-15 22:50-23:20 UTC | validation, earlier | 43.0% (119/277) | 0.7% (2/282) | 2.81 |
| Gulf 2026-10-12 05:00-05:15 UTC | validation, earlier | 86.6% (103/119) | 0.8% (1/121) | 0.15 |
| Gulf 2026-10-11 05:45-06:00 UTC | validation, earlier | 81.0% (111/137) | 0.0% (0/140) | 0.16 |
| Gulf 2026-10-16 04:45-05:00 UTC | development | 82.4% (84/102) | 1.0% (1/105) | 0.25 |
| Gulf 2026-10-16 04:10-04:25 UTC | development | 78.4% (76/97) | 1.0% (1/105) | 0.21 |
| Gulf 2026-10-13 05:30-05:45 UTC | development | 85.0% (108/127) | 0.8% (1/132) | 0.15 |
| Gulf 2026-10-14 06:15-06:30 UTC | development | 82.4% (108/131) | 0.7% (1/134) | 0.14 |
| Gulf 2026-10-10 02:01-02:16 UTC | development | 77.7% (94/121) | 0.0% (0/125) | 0.22 |
| NYC 2026-10-16 17:52-18:07 UTC | development | 65.0% (115/177) | 0.0% (0/181) | 1.20 |
| NYC 2026-10-10 02:38-03:08 UTC | development | 34.9% (51/146) | 1.3% (2/150) | 2.57 |

Kinematic is a strong secondary signal on Gulf traffic (76-91%, 83-90%
on the validation windows) since fusion measures ADS-B velocity, and a
weak one at NYC (35-43%): with 11s updates a deviation has to clear 4
sigma within 33s, and approach traffic turns all the time (61.5-65% on
the daytime NYC capture, which has more cruising traffic). No stated
target; false alarms under 3% everywhere.

### Realistic radar (2026-10-18; re-scored 2026-10-22)

Everything above uses the original simulated radar: every aircraft
seen, 150 m everywhere, nothing false. `--radar model` re-runs a window
with ingestion/radar_model.py's radar instead -- one site per region,
accuracy that degrades with range, a horizon, and 12 false contacts a
minute (ingestion/README.md). Same validation windows, scored after the
code was frozen:

| Window | Went-dark recall | False-positive rate | Kinematic recall | Coverage gaps skipped |
|---|---|---|---|---|
| NYC 2026-10-16 18:08-18:23 UTC | **98.3%** (119/121) | **0.0%** (0/165) | 64.0% (103/161) | 2 |
| Gulf 2026-10-12 04:00-04:15 UTC | **98.1%** (52/53) | **0.0%** (0/111) | 64.5% (69/107) | 57 |
| Gulf 2026-10-13 02:00-02:15 UTC | **89.6%** (43/48) | **0.0%** (0/106) | 57.1% (60/105) | 56 |
| Gulf 2026-10-14 04:30-04:45 UTC | **91.2%** (52/57) | **0.0%** (0/102) | 68.0% (68/100) | 44 |

**Re-scored:** the first version of this table (93.0-99.2% went-dark,
0-2.8% false alarms) came from radar that wasn't reproducible after
all. The database returns rows with equal timestamps in no fixed order,
so the "seeded" draws landed on different rows each run. Ties are now
broken by id, and a test pins it. The table above is the reproducible
draw. With ~50 scorable Gulf aircraft per window, one draw of the radar
can move recall by a few points. Track confirmation, added in the same
change, has no effect on these numbers (same 43/48 and 52/57 with it
off).

- **Went-dark holds up where the radar can see** (89.6-98.3%). About
  half the Gulf aircraft are skipped as coverage gaps: one 200 nmi radar
  doesn't cover a 1,000 km box, and a plane the radar can't see going
  dark is undetectable by construction. Gulf 10-13 falls just under 90%.
- **No false alarms on ordinary traffic.** On the development windows
  this radar first gave 3-5%, and every one traced to the same thing: a
  clutter contact accepted 10-30 km from a track coasting through an
  ordinary transponder pause, so the silent track looked
  radar-corroborated. A coasting constant-velocity track's gate opens to
  ~12 km after 90 s. Fix: cap the gate at 10 km (~3x the measured p95
  prediction error), plus a background-density test
  (fusion/associate.py). Height gating, the other candidate, made it
  worse (7.6%).
- **Kinematic drops on Gulf traffic** (83-90% -> 57-68%) **entirely from
  coverage**: split on the development windows, aircraft the radar sees
  on at least half their fixes scored the same as before (21/27 vs
  21/27, 62/76 vs 61/76); the drop is all aircraft outside it, where
  ADS-B alone has to show the maneuver. NYC, inside one terminal radar,
  is unchanged (61.5% -> 64.0%).

### Alert episodes (2026-10-16)

One anomaly should be one alert. Both detectors used to re-alert:

- **Went-dark** closed an episode whenever the track momentarily failed
  went_dark() -- routinely, since at 45 s cadence with 10% radar
  dropout a radar gap past the recency limit is common -- and reopened
  it on the next contact. YETI45 (the demo, docs/DEMO.md) alerted at
  +54 s, +195 s and +303 s for one dropout. Episodes now stay open until
  ADS-B actually resumes or the track is retired
  (`detectors.went_dark_episodes`, used by the live engine, the harness
  and the replay API).
- **Kinematic** kept episodes per (track, sensor), so one maneuver could
  alert from ADS-B and again from radar, and again after any single
  normal update. Episodes are now per track and close after
  `consecutive` normal updates, the same bar it takes to open one.

Recall and the false-positive rate are unchanged by construction --
the first detection is the same -- and measured unchanged. What
changes is the "induced" column: for went-dark it was almost entirely
the target re-alerting, and drops from 0.5-2.5 per run to 0.00-0.20
on every window. The tables above are re-runs with both changes,
checked row by row: recall and false-positive rates identical.

### Velocity measurement (2026-10-16 round)

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
| + ADS-B velocity measured, 50 m ADS-B position noise | 90.1% / 0.0% | 97.6% / 0.0% |
| + scoring skips coverage gaps (12 NYC, 0 Gulf injections) | **96.0% / 0.0%** | **97.6% / 0.0%** |

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
- **Induced kinematic alerts at NYC (~2.6 / run) are the target
  itself**, re-alerting during its own maneuver -- all 98 in a 40-run
  sample. The injected 2g push accelerates without end, so the filter
  absorbs it, looks normal for two updates, and the push pulls away
  again: a new episode each time. An artifact of the injection (next
  item), not of the detector. (Earlier versions of this README blamed
  neighbouring tracks; that was wrong -- see "Alert episodes".)
- **The injected 2g maneuver is a sustained push in one direction** --
  displacement grows as t^2 without bound, unlike a real turn. Fine as a
  "can the detector see it" test; not a model of real flight.
- **Kinematic at NYC cadence stays at 35-43%** -- see above.
