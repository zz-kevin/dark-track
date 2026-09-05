# ingestion/

Two independent producers writing into a shared raw-track schema:

1. **OpenSky poller** — real ADS-B state vectors (callsign, lat/lon,
   altitude, velocity) for a bounding box, pulled from the free OpenSky
   Network REST API. This is the cooperative, ID'd source.
2. **Synthetic radar generator** — takes the OpenSky truth positions and
   produces a second, degraded feed: Gaussian position noise, latency
   jitter, random dropout, and no ID field. This asymmetry is what creates a
   genuine fusion problem instead of a single-feed dashboard.

Also owns **replay**: raw ticks from both sources get stored so any session
can be re-run deterministically later, for both testing and demos.

## Setup

```
python3 -m venv ../.venv && source ../.venv/bin/activate
pip install -r ../requirements.txt
createdb dark_track            # or set DATABASE_URL to point elsewhere
python3 db.py                  # creates the raw_ticks table
```

## Running it

```
python3 opensky_poll.py                 # writes source='adsb' rows
python3 radar_sim.py                    # derives source='radar' rows from those
python3 replay.py <start-iso> <end-iso> # re-emit a stored window, --fast to skip real-time pacing
```

Run the poller first — `radar_sim.py` only processes adsb ticks written
after it starts (or pass `--since-id` to backfill from a specific row).

## Files

| File | Purpose |
|---|---|
| `schema.sql` | The shared `raw_ticks` table both producers write into |
| `db.py` | Connection + insert/query helpers (`DATABASE_URL` env var, defaults to local `dark_track`) |
| `opensky_poll.py` | Real ADS-B source — polls OpenSky, prints + stores state vectors |
| `radar_sim.py` | Synthetic radar source — noise, latency, dropout, no ID, derived from stored adsb ticks |
| `replay.py` | Re-emits a stored time window, interleaved by `observed_at` |

## Status

- [x] Week 1 — OpenSky poller proof-of-life
- [x] Weeks 2–4 — shared schema, radar simulator, replay
- [ ] Next: validate fusion accuracy assumptions against real dropout/noise rates before tuning `fusion/`'s gating window
