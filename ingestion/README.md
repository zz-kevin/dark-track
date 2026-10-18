# ingestion/

Two independent producers writing into a shared raw-track schema:

1. **OpenSky poller** — real ADS-B state vectors (callsign, lat/lon,
   altitude, velocity) for a bounding box, pulled from the free OpenSky
   Network REST API. This is the cooperative, ID'd source.
2. **Synthetic radar generator** — takes the OpenSky truth positions and
   produces a second, degraded feed: Gaussian position noise, latency
   jitter, random dropout, and no ID field. This asymmetry is what creates a
   genuine fusion problem instead of a single-feed dashboard.

Tags every ADS-B tick with its transponder address (`icao24`) and, if it's
one of the **watched military types** below, its `aircraft_type` /
`aircraft_variant` (see `aircraft_types.py`).

Also owns **replay**: raw ticks from both sources get stored so any session
can be re-run deterministically later, for both testing and demos.

## Setup

```
python3 -m venv ../.venv && source ../.venv/bin/activate
pip install -r ../requirements.txt
createdb dark_track            # or set DATABASE_URL to point elsewhere
python3 db.py                  # creates the raw_ticks table
```

## Watched aircraft types

| Key | Aircraft | Category | Role |
|---|---|---|---|
| `F15E` | F-15E Strike Eagle | manned | Primary strike fighter |
| `F35` | F-35 Lightning II (variant `A` land-based, `C` carrier) | manned | Stealth strike / surveillance |
| `A10` | A-10 Thunderbolt II | manned | Close air support, low-altitude cover for rescue ops |
| `F16` | F-16 Fighting Falcon | manned | Strike and patrol from regional bases |
| `EA18G` | EA-18G Growler | manned | Electronic warfare: radar/comms jamming |
| `B2` | B-2 Spirit | manned | Stealth bomber, precision strike |
| `HC130J` | HC-130J Combat King II | support | Airborne command post for personnel recovery / SAR |
| `H60` | HH-60W Jolly Green II / UH-60 Black Hawk (variant `HH-60W` / `UH-60`) | support | Combat rescue and transport |
| `E3` | E-3 Sentry (AWACS) | support | Airborne warning and control |
| `E2D` | E-2D Advanced Hawkeye | support | Carrier-based early warning and control |
| `P8` | P-8A Poseidon | support | Maritime patrol, Gulf / Strait of Hormuz |
| `FA18EF` | F/A-18E/F Super Hornet (variant `E` / `F`) | naval | Carrier-based strike fighter |
| `MQ9` | MQ-9 Reaper | drone | Surveillance and precision strike |
| `MQ4C` | MQ-4C Triton | drone | High-altitude long-endurance maritime/overland ISR |
| `RQ180` | RQ-180 | drone | Stealth penetrating ISR — **not trackable on ADS-B**, listed only |
| `LUCAS` | LUCAS one-way attack drone | drone | Loitering munition — **not trackable on ADS-B**, listed only |
| `US_MIL` | US military hex block (`AE0000`-`AFFFFF`), no type match | — | Fallback flag |

**Naval air power:** the carrier air wing (F/A-18E/F, F-35C, EA-18G,
E-2D) operating from USS Abraham Lincoln (CVN-72), USS Gerald R. Ford
(CVN-78) and USS George H.W. Bush (CVN-77). The carriers themselves aren't
tracked: warships don't broadcast ADS-B and usually run AIS dark, so
they're listed in `aircraft_types.CARRIERS` for reference only.

RQ-180 and LUCAS have no transponder and no public type designator, so
the classifier never returns them. If they show up at all, it's as a
radar-only track with no ADS-B identity.

OpenSky state vectors carry no airframe type, so identification joins
`icao24` against OpenSky's public aircraft metadata database. Download the
CSV once from <https://opensky-network.org/datasets/metadata/> into
`data/raw/aircraft-database.csv` (gitignored). The poller picks it up
from there automatically, or pass a different path with `--aircraft-db`. Matching needs
the ICAO type designator; types whose designator is shared with
non-watched airframes (EA-18G vs. Super Hornet, HC-130J vs. other C-130Js,
HH-60W/UH-60 vs. Seahawks) also need a model-string match.

Honest caveat: these aircraft mostly fly with ADS-B off during real
operations, and the metadata DB is crowd-sourced, so coverage is partial.
A watched type whose transponder drops while the radar contact keeps
flying is exactly the went-dark case `anomaly/` detects.

## Running it

```
python3 opensky_poll.py                 # writes source='adsb' rows
python3 opensky_poll.py --region gulf --aircraft-db ../data/raw/aircraft-database.csv
python3 opensky_poll.py --bbox 23 30.5 47.5 57.5 --watched-only
python3 opensky_poll.py --region gulf --quiet   # only counts + watched aircraft
python3 opensky_poll.py --region gulf --quiet --poll-seconds 60   # stretch anonymous credits
../poll.sh --region gulf --quiet   # from any directory; refuses to start a second poller
python3 radar_sim.py                    # derives source='radar' rows from those
python3 radar_sim.py --model realistic --region gulf   # one radar site: range, horizon, clutter
python3 replay.py <start-iso> <end-iso> # re-emit a stored window, --fast to skip real-time pacing
python3 replay.py <start-iso> <end-iso> --type F35 --type B2   # watched types only
```

The poller survives OpenSky rate limiting (HTTP 429: it waits out the
reset time OpenSky reports) and Postgres being down (that poll's ticks
are dropped with a warning, polling continues).

**OpenSky limits:** anonymous access runs out within about an hour on
`--region gulf` at 10s polls. For more, create a free OpenSky account,
add an API client on your account page, and export its credentials
before starting the poller (never put them in code or flags):

```
export OPENSKY_CLIENT_ID=...
export OPENSKY_CLIENT_SECRET=...
```

Duplicate ADS-B ticks (same `icao24` + `observed_at`) are rejected by a
unique index, so an accidental second poller can't double-store data.

Run the poller first — `radar_sim.py` only processes adsb ticks written
after it starts (or pass `--since-id` to backfill from a specific row).

## Files

| File | Purpose |
|---|---|
| `schema.sql` | The shared `raw_ticks` table both producers write into |
| `db.py` | Connection + insert/query helpers (`DATABASE_URL` env var, defaults to local `dark_track`) |
| `opensky_poll.py` | Real ADS-B source — polls OpenSky, prints + stores state vectors, flags watched types |
| `aircraft_types.py` | Watched military aircraft list + icao24 -> type classifier |
| `radar_sim.py` | Synthetic radar source — noise, latency, dropout, no ID, derived from stored adsb ticks |
| `replay.py` | Re-emits a stored time window, interleaved by `observed_at` |
| `regions.py` | The `--region` bounding boxes; splits a stored window back out by region |
| `radar_model.py` | The realistic radar: a notional site per region, range-dependent accuracy, detection falloff, radar horizon, clutter |
| `radar_truth.py` | Evaluation only: which ADS-B tick (so which aircraft) each radar contact came from |

Radar contacts carry `truth_tick_id`, the ADS-B row they were derived
from -- simulation ground truth for scoring (`fusion/accuracy.py`,
`anomaly/inject.py`). Fusion never reads it. Contacts written before the
column existed are matched back by `radar_truth.py` (99.2% of NYC and
100% of Gulf contacts labeled unambiguously; the rest are left unscored).
Run `python3 db.py` once to add the column to an existing database.

Radar contacts carry a simulated height error (default 300 m,
`radar_sim.py --height-noise-std-m`) and record it in `altitude_std_m`.
Contacts written before 2026-10-16 ~06:30 UTC carry the ADS-B altitude
exactly (`altitude_std_m` NULL); replay tools add the same error when
they read them (fusion/simulate.py).

## The realistic radar (`radar_model.py`)

`radar_sim.py`'s original radar is the same everywhere: 150 m of noise
on every contact, 10% dropped at random, every aircraft seen, nothing
seen that isn't there. `radar_model.py` puts one radar at a notional site
per region and gives it what makes real fusion hard:

| | NYC terminal radar | Gulf long-range radar |
|---|---|---|
| Site (notional) | 40.70 N, 73.85 W, 30 m antenna | 25.90 N, 51.60 E (Qatar), 60 m antenna |
| Range | 111 km (60 nmi) | 370 km (200 nmi) |
| Accuracy | 50 m in range, 0.15 deg in bearing: ~50 m at 20 km, ~370 m per axis at 200 km |
| Height | 0.25 deg in elevation, at least 150 m |
| Detection | 95% close in, halving at 85% of max range; none below the radar horizon (4/3-earth) |
| Clutter | 12 false contacts a minute, anywhere in coverage |

Each contact records its own accuracy (`noise_std_m`, `altitude_std_m`),
and fusion weights it by that instead of a fixed 150 m. Clutter is
written with `truth_tick_id = -1` (`radar_truth.py` counts it, never
labels it).

Live: `radar_sim.py --model realistic [--region gulf]`. Stored windows:
every replay and scoring tool takes `--radar model`
(`fusion/accuracy.py`, `fusion/bench.py`, `anomaly/harness.py`; the UI's
"Radar" choice), which regenerates the window's radar from its ADS-B
with this model -- seeded, so the same window always gets the same
contacts -- before anything is injected. What it changed is in
docs/FUSION.md ("Realistic radar").

The sites are notional, placed for coverage, not real installations;
and contacts are still derived from ADS-B fixes, so an aircraft that
never transmitted at all still isn't in the data.

ADS-B rows also carry `velocity_at` (OpenSky's `last_contact`): the
reported speed/heading come from the aircraft's latest message, usually
~2.5 s newer than the position fix in `observed_at`. Recorded since
2026-10-16 04:07 UTC; see docs/FUSION.md for what fusion does (and
doesn't) do with it.

## Status

- [x] Week 1 — OpenSky poller proof-of-life
- [x] Weeks 2–4 — shared schema, radar simulator, replay
- [x] Ground truth for radar contacts (`truth_tick_id`, `radar_truth.py`), used to measure fusion accuracy
- [x] Realistic radar model (`radar_model.py`): site, range-dependent accuracy, horizon, clutter
- [x] Fresh NYC validation captures, 2026-10-15 22:50-23:20 and 2026-10-16 01:30-02:00 UTC (radar with exact `truth_tick_id`)
