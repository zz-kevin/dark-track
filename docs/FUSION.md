# Fusion design notes

## State estimation

**Filter chosen: constant-velocity Kalman filter** (`fusion/kalman.py`), one
instance per fused track, state `[x, y, vx, vy]` in local flat-earth ENU
meters (`fusion/geo.py`), not lat/lon degrees.

**Why Kalman over alpha-beta:** the actual point of this project is
combining a trustworthy source with an untrustworthy one into "one
confident picture" — that requires representing *how much* to trust each
measurement. A Kalman filter's per-update measurement noise `R` does this
directly: ADS-B updates use `R_adsb` (std dev ~20m), radar updates use
`R_radar` (std dev ~150m, matching `radar_sim.py`'s injected noise). The
filter's gain naturally weights a trusted ADS-B fix far more than a noisy
radar one on the same track, and still lets radar alone keep updating the
track's position when ADS-B goes quiet. An alpha-beta filter has no
natural per-measurement confidence input — you'd have to hand-tune
separate alpha/beta constants per source, which is really just a Kalman
filter with the covariance bookkeeping thrown away.

Only position is measured (`H` selects `x, y`); velocity/heading carried
on raw ticks is not fed into the filter as a measurement -- but it does
seed a *new* track's initial velocity (with a tighter initial velocity
variance), so a just-created track predicts forward instead of standing
still until its second position fix (98.4% -> 99.2% radar association on
Gulf data). The flat-earth reference point (`geo.py`) is set at engine
startup to the center of recent ADS-B data rather than fixed at NYC.
Otherwise, so both sources share the same
measurement model. Process noise is a discretized white-noise-acceleration
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
- **Staleness gate** (`--gate-staleness-s`, default 30s or twice the
  measured ADS-B update interval, whichever is longer): a track not
  updated within this window isn't considered a live candidate at all,
  identified or not.

ADS-B ticks that carry a callsign skip gating entirely — callsign is a
ground truth identity, so they update-or-create their track directly by a
callsign -> track_id map.

## Association

**Algorithm used: Hungarian algorithm** (`scipy.optimize.linear_sum_assignment`)
over the batch of anonymous ticks in one poll cycle vs. the current gated
track set, minimizing total assignment distance.

**Alternatives considered:** greedy nearest-neighbor (repeatedly claim the
closest remaining pair). Rejected because `radar_sim.py` derives every
poll cycle's radar contacts from the same OpenSky snapshot, so multiple
ambiguous ticks routinely land in the same batch — exactly the case where
greedy assignment can steal a good match away from a tick that had no
other reasonable candidate, and Hungarian's whole-batch optimum doesn't.
The cost of this correctness is `O(n^3)` in the (small) batch size, which
is irrelevant at this project's track counts.

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
  batches instead of one giant one.
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
- **Gating/association ignores velocity.** Cost is pure position
  distance; two crossing aircraft moving in very different directions
  but momentarily close together would be gated as candidates for each
  other with no velocity-consistency check to break the tie. The Kalman
  filter's own covariance-weighted (Mahalanobis) distance would handle
  this more rigorously than the fixed-radius Euclidean gate used here.
  *Partly done:* the gate is now the larger of the 800m radius and a
  chi-square (99.9%) gate on the innovation covariance, added because at
  45s ADS-B cadence the fixed radius rejected most radar contacts on a
  track that had just gone dark (see anomaly/README.md). Assignment cost
  is still plain Euclidean distance, so the crossing-aircraft case above
  is unchanged.
