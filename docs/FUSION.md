# Fusion design notes

## State estimation

**Filter chosen: constant-velocity Kalman filter** (`fusion/kalman.py`), one
instance per fused track, state `[x, y, vx, vy]` in local flat-earth ENU
meters (`fusion/geo.py`), not lat/lon degrees.

**Why Kalman over alpha-beta:** the actual point of this project is
combining a trustworthy source with an untrustworthy one into "one
confident picture" — that requires representing *how much* to trust each
measurement. A Kalman filter's per-update measurement noise `R` does this
directly: ADS-B updates use `R_adsb` (std dev 50m -- see below), radar updates use
`R_radar` (std dev ~150m, matching `radar_sim.py`'s injected noise). The
filter's gain naturally weights a trusted ADS-B fix far more than a noisy
radar one on the same track, and still lets radar alone keep updating the
track's position when ADS-B goes quiet. An alpha-beta filter has no
natural per-measurement confidence input — you'd have to hand-tune
separate alpha/beta constants per source, which is really just a Kalman
filter with the covariance bookkeeping thrown away.

**What's measured.** Radar measures position only. ADS-B measures
position *and* velocity: the reported ground speed and track feed the
filter on every airborne ADS-B update (`H` = identity, velocity std
10 m/s per axis, `engine.ADSB_VEL_STD_MPS`). Reported velocity also seeds
a *new* track's velocity from either source, so a just-created track
predicts forward instead of standing still (98.4% -> 99.2% radar
association on Gulf data when that was added).

Measuring velocity pins down exactly what a 45s prediction depends on.
Before, the filter had to infer velocity from positions, and a radar
contact arriving 1-4 s after an ADS-B fix, with 150 m of noise, could
swing it by tens of m/s -- Gulf ADS-B residuals were ~1 km at every
update. Measured on validation windows nothing was tuned on: Gulf fused
position error p50 46-49 m -> 24-25 m, accuracy +2 points, went-dark
recall up 1-3 points with false alarms down, kinematic recall 15-26% ->
73-93% (a maneuver now shows up as a velocity the filter believes, not
just a position it might put down to noise). Numbers by window in
anomaly/README.md and below.

Two things had to come with it, both found on the development windows:

- **ADS-B position noise 20 m -> 50 m** (`engine.R_ADSB_STD_M`). An ADS-B
  fix is good to ~10 m, but OpenSky's position timestamps jitter by
  seconds -- position-implied speed swings +/-20-30 m/s between fixes --
  which at airliner speeds is tens of metres of effective error. At
  20 m, with velocity also pinned, the filter had nowhere to put that
  jitter, and NYC accuracy fell (82.8% -> 77.5%). 50 m alone (no
  velocity) was the best NYC setting found (87.4%), and with velocity
  it kept Gulf's gains.
- **No velocity measurement on the ground** (`engine.VEL_SKIP_GROUND`,
  no barometric altitude): a taxiing aircraft's reported track at
  walking pace is noise. NYC 81.5% -> 84.1% on the development window.

The trade-off taken: on NYC all-traffic accuracy, position-noise-only
(87.4% on the development window) beats the chosen setting (84.1%); the
chosen setting is ahead on everything else, and NYC airborne accuracy
stays above 90% either way. NYC's all-traffic number is dominated by
ground traffic that position alone can't resolve (see Measured accuracy).

The flat-earth reference point (`geo.py`) is set at engine startup to
the center of recent ADS-B data rather than fixed at NYC. Process noise is a discretized white-noise-acceleration
model (`accel_std`, default 3 m/s²) — large enough to follow real turns
and speed changes, tuned by eye rather than against a labeled maneuver
dataset (see Known limitations).

## Gating

Before a tick without an identity (a radar contact, or the rare
blank-callsign ADS-B tick) can be assigned to a track, it must pass two
independent gates against that track's *predicted* position at the tick's
timestamp (`fusion/associate.py`):

- **Distance gate** (`--gate-dist-m`, default 800m, widened by a
  covariance gate when the prediction is genuinely that uncertain — see
  `associate.py`): roughly 5x the
  radar's injected noise std dev — generous enough to survive one bad
  noise draw, tight enough to reject genuinely different aircraft in
  busy airspace.
- **Staleness gate** (`--gate-staleness-s`, default 30s or 2.5x the
  measured ADS-B update interval, whichever is longer): a track not
  updated within this window isn't considered a live candidate at all,
  identified or not. 2.5 intervals = one revisit, one *dropped* revisit,
  and latency jitter: at 2x, a single dropped radar contact (radar_sim
  drops 10%) left a just-gone-dark Gulf track out of reach of the next
  one (94s vs. a 90s gate), and it never got radar again.

ADS-B ticks that carry a callsign skip gating entirely — callsign is a
ground truth identity, so they update-or-create their track directly by a
callsign -> track_id map.

## Association

**Algorithm used: Hungarian algorithm** (`scipy.optimize.linear_sum_assignment`)
over the batch of anonymous ticks in one poll cycle vs. the current gated
track set, minimizing total negative log-likelihood: for each pair,
squared Mahalanobis distance under the track's predicted innovation
covariance S, plus log det S (`associate.COST`).

**Why likelihood, not distance:** the cost was plain Euclidean distance
until 2026-10-15. When ADS-B drops at 45s cadence, the dark track's
prediction is typically ~1 km off but honestly uncertain (~4 km std); a
neighbour updated seconds ago, 600 m away with a tight covariance, has
the smaller distance and took the contact (traced: ETD607 losing its
first post-dropout contact to UAE2P -- distance 623 m vs. 1733 m, but
Mahalanobis 16.4 vs. 0.4). The log det S term keeps a vague track from
claiming everything: between two equally plausible tracks, the more
certain one wins.

**Alternatives considered:** greedy nearest-neighbor (repeatedly claim the
closest remaining pair). Rejected because `radar_sim.py` derives every
poll cycle's radar contacts from the same OpenSky snapshot, so multiple
ambiguous ticks routinely land in the same batch — exactly the case where
greedy assignment can steal a good match away from a tick that had no
other reasonable candidate, and Hungarian's whole-batch optimum doesn't.
The cost of this correctness is `O(n^3)` in the (small) batch size, which
is irrelevant at this project's track counts.

**Time-sliced batches.** Anonymous ticks are associated in 1 s slices
of the poll batch, in time order (`engine.ASSOCIATION_SLICE_S`), not as
one batch. Hungarian assignment is one-to-one, so two contacts from the
*same* aircraft in one batch can't both join its track and the loser
spawns a phantom track. At NYC's 11s cadence 8% of revisits fall within
one 3s poll; measured on that window, 89 of the 110 airborne contacts
that missed their own track had lost it to the same aircraft's other
contact. Slicing took NYC from 64.2% to 77.5% of aircraft correctly
associated (78.6% -> 89.3% airborne); Gulf (45s cadence) is unchanged.

**Gating is vectorized** (`associate.py`): each tick is gated against
every track's predicted position and covariance in one numpy pass --
the same arithmetic as `KalmanFilter.peek_predict`, verified to produce
identical assignments on 3,017 NYC ticks, ~30x faster. The per-pair loop
was the slowest part of a cycle (p95 360 ms on a Gulf burst).

## Conflict resolution

Each poll cycle processes identified ADS-B ticks first (unambiguous,
updates tracks directly), then batch-associates everything anonymous
(radar contacts + any blank-callsign ADS-B) against the resulting track
set. **This is a documented simplification, not a fully time-ordered
merge of both streams** — see Known limitations for the failure mode it
causes.

- **Source disagreement:** resolved by the Kalman gain, not by an
  explicit rule — a well-corroborated ADS-B track barely moves in
  response to a noisy radar update; a radar-only track (ADS-B gone dark)
  moves however the radar says it did.
- **Dropout:** a track with no update in `--track-ttl-s` (default 120s)
  is retired — deleted from `fused_tracks`, removed from the callsign
  map. `fused_track_events` keeps the full history regardless.
- **Reappearance under a new ID:** not handled. If ADS-B drops and comes
  back with the same callsign before the track is retired, it resumes
  updating the same track normally. If it reappears as a genuinely new
  callsign (or after the track was already retired), it's treated as a
  brand new track with no link back to the old one — there's no re-ID
  logic. This is explicitly the anomaly module's territory: the gap
  between `last_adsb_at` and `last_radar_at` that fusion exposes per
  track is what a went-dark detector (weeks 10-11) would threshold on.

## Confidence scoring

Heuristic, not a calibrated probability (`Track.confidence()` in
`fusion/track.py`): recent ADS-B corroboration (<15s) scores highest,
recent radar-only corroboration lower, anything older lower still, each
further discounted by the filter's own position covariance. Stored as a
snapshot at the time a track was last touched, not recomputed live — a
track that stops being updated keeps its last-computed confidence in
`fused_tracks` until it's retired, rather than decaying continuously.

## Measured accuracy

`fusion/accuracy.py` scores association against ground truth for every
synthetic radar contact (`ingestion/radar_truth.py` recovers which
aircraft each one came from). Criterion from docs/PROBLEM.md: a contact
is correct if it updated its own aircraft's track, the fused position
right after is within 300 m of the truth, and it was fused within 5 s;
an aircraft passes at >= 90% correct contacts.

| Window | Role | Aircraft passing | Airborne only | Contacts correct | Fused error p50 / p95 |
|---|---|---|---|---|---|
| NYC 2026-10-16 01:30-02:00 | validation | **81.2%** (173/213) | 93.0% | 94.9% | 25 / 140 m |
| Gulf 2026-10-13 20:00-20:15 | validation | **98.5%** (132/134) | 99.2% | 99.6% | 25 / 59 m |
| Gulf 2026-10-12 20:30-20:45 | validation | **97.1%** (135/139) | 97.0% | 99.5% | 24 / 58 m |
| Gulf 2026-10-16 04:30-04:45 | validation | **91.7%** (66/72) | 91.8% | 96.9% | 29 / 147 m |
| Gulf 2026-10-16 04:45-05:00 | validation | **93.8%** (122/130) | 94.3% | 96.4% | 25 / 84 m |
| NYC 2026-10-15 22:50-23:20 | validation, earlier | 89.8% (272/303) | 98.5% | 95.2% | 24 / 87 m |
| Gulf 2026-10-12 05:00-05:15 | validation, earlier | 98.6% (140/142) | 98.6% | 99.7% | 24 / 54 m |
| Gulf 2026-10-11 05:45-06:00 | validation, earlier | 100.0% (159/159) | 100.0% | 100.0% | 23 / 56 m |
| Gulf 2026-10-16 04:10-04:25 | development | 93.1% (121/130) | 94.2% | 97.7% | 26 / 76 m |
| Gulf 2026-10-13 05:30-05:45 | development | 98.1% (156/159) | 98.7% | 99.6% | 23 / 59 m |
| Gulf 2026-10-14 06:15-06:30 | development | 99.3% (151/152) | 100.0% | 99.7% | 24 / 60 m |
| Gulf 2026-10-10 02:01-02:16 | development | 99.3% (146/147) | 99.3% | 99.8% | 24 / 53 m |
| NYC 2026-10-10 02:38-03:08 | development | 84.1% (127/151) | 92.4% | 95.0% | 27 / 141 m |

Roles as in anomaly/README.md. Gulf clears the 90% target everywhere
(91.7-100%; the Gulf morning capture is the low end). NYC airborne does too (92.4-98.5%); NYC all-traffic
doesn't (81.2-89.8%), because of ground traffic -- the 2026-10-16
capture, at night, has proportionally more of it.

Earlier round: the likelihood cost (vs. Euclidean, same everything else) took the
fresh NYC capture from 83.2% to 87.8% and the NYC tuning window from
77.5% to 82.8%; on Gulf the two are within one aircraft of each other.

Where NYC loses it: almost entirely on the ground. On the tuning window the airborne
misses were mostly fused positions > 300 m off during approach turns (a
constant-velocity filter lags a turn); on the ground,
contacts swapped between taxiing neighbours (254 of the 257 ground
misses landed on another aircraft's track). Gulf's worst cases are
departures accelerating 43 -> 70 m/s between 45s updates, and formation
pairs (A04/A05).

## Known limitations

- **Batch order can starve an out-of-order gate.** Processing all of a
  cycle's identified ADS-B ticks before any anonymous tick means a
  track's clock can be advanced past a radar tick's timestamp before that
  radar tick is even evaluated (`age` goes negative in `associate.py`).
  Handled by clamping: `abs(age) > gate-staleness-s` is the actual reject
  condition, not `age < 0`, so small negative gaps are tolerated using the
  filter's current estimate. This holds up fine in live operation (one
  new tick per aircraft per cycle) but breaks down for a large
  `--since-id` backfill of many historical polls in a single pass:
  repeated radar contacts from the same aircraft's earlier polls end up
  competing for one already-collapsed track, and only one can win a
  one-to-one Hungarian assignment. Measured directly: a 66-tick/61-contact
  historical backfill run in one cycle matched only ~26% of radar
  contacts; the same data replayed live (small poll cycles) matched ~74%.
  Fix if it matters later: process a backfill in small time-sliced
  batches instead of one giant one. (Anonymous ticks are now sliced
  within a batch -- see Association -- but identified ADS-B still runs
  first, so a multi-poll backfill still advances tracks past their own
  radar contacts.)
- **No smoothing pass.** This is a forward-only filter; a real system
  doing offline replay analysis would run an RTS smoother backward over
  a completed track for a better historical estimate. Not implemented —
  out of scope for live tracking.
- **No re-identification after track loss.** Covered above under
  reappearance — a track that goes fully stale and gets retired has no
  path back to its old identity if it returns.
- **Confidence is a hand-tuned heuristic**, not fit or validated against
  labeled data — there's no ground truth for "this track's identity is
  actually X% likely correct" at this project's scale.
- **Gating/association ignores velocity.** Two crossing aircraft moving
  in very different directions but momentarily close together are still
  candidates for each other; position covariance is all the cost sees.
  *Done:* covariance gate (chi-square 99.9%) alongside the 800 m radius,
  and a likelihood assignment cost (see Association). Together with the
  wider staleness gate, held-out went-dark recall went 84.0% -> 94.7%
  and 89.2% -> 93.8% (anomaly/README.md). What's left is velocity:
  nothing in the cost compares a contact's reported heading with the
  track's.
- **One flat-earth plane per process.** `geo.py` is valid at regional
  scale; two regions in one run (both pollers on at once) put the
  reference point between them and distort distances by ~10%. Replay
  tools take `--region`; the live engine warns and takes `--region` too.
- **Constant-velocity model.** Turns and accelerating departures are
  followed late, less so now that ADS-B velocity is measured. Once
  ADS-B drops, the track coasts on its last velocity and radar alone, so
  a dropout *during a turn* is the hardest case: on a morning-arrivals
  Gulf window (2026-10-16 04:10), the dark track's prediction was ~4 km
  off 50-64 s later, and its first radar contact went to a neighbour
  (or, for FRS41, its formation partner FRS42, whose ADS-B-backed track
  then absorbed every later contact). An interacting-multiple-model
  filter (constant-velocity + coordinated-turn) is the standard fix;
  not attempted yet.
- **Velocity/position time offset: measured, not used.** OpenSky's
  velocity is from the latest message, ~2.5 s (up to a minute) newer
  than the position fix. The poller now records that time
  (`raw_ticks.velocity_at`, from `last_contact`), and the filter can
  inflate the velocity noise by `accel_std * offset`
  (`engine.VEL_TIME_OFFSET`). On the first window with the column
  (Gulf 2026-10-16 04:10) it made things slightly worse -- accuracy
  93.1% -> 90.8%, kinematic recall 78.4% -> 73.2%, went-dark +1 point --
  so it's off. It was aimed at NYC approach turns; it needs an NYC
  capture with `velocity_at` to judge there.
- **Ground traffic is a sensor limit.** Of the NYC ground radar
  contacts that landed on another aircraft's track, 93% (158/169) were
  *more likely* under that aircraft's predicted position than under
  their own -- the radar noise (150 m) put them there, and no
  assignment rule using position can do better. Median true separation
  between the two aircraft: 245 m. Skipping velocity only below taxi
  speed (so takeoff rolls keep it) changed nothing measurable. Went-dark
  ignores ground traffic regardless.
