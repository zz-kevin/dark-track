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

_(Fill these in for real once ingestion is running — these are starting
targets, not final numbers.)_

- **Fusion accuracy:** correctly associate the same real-world aircraft
  across both sources within `___` meters and `___` seconds, for `___`% of
  tracks in a test window.
- **Anomaly detection:** flag an injected "went dark" event within `___`
  seconds of it occurring, with a false-positive rate under `___`% on a
  held-out replay set.
- **Throughput:** sustain fusion + anomaly detection for at least `___`
  concurrent tracks with end-to-end (sensor tick → fused output) latency
  under `___` ms.

## Non-goals

- Not building a production-grade distributed system. A single process is a
  legitimate architecture for the whole semester unless there's a specific
  reason to split it.
- Not integrating classified, proprietary, or access-controlled data of any
  kind. Everything here runs on public APIs and self-generated synthetic
  data.
- Not chasing 100% detection accuracy — a measured, honest precision/recall
  number beats an unverified claim of "it works."
