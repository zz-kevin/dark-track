# Dark Track

Real-time fusion of two independent, noisy sensor feeds — real ADS-B aircraft
data and a simulated non-cooperative radar contact — into a single tracked
picture, with anomaly detection for aircraft that behave like they're trying
not to be seen (transponder drops while the radar-like contact keeps flying
the same profile).

Solo capstone project. Status: **week 1 — scaffolding.**

## Why two sources

A single ADS-B feed is just a flight tracker. The interesting engineering
problem — track association, state estimation, conflict resolution — only
shows up once you have two disagreeing, uncorrelated views of the same
airspace. See [`docs/PROBLEM.md`](docs/PROBLEM.md) for the full framing and
success criteria.

## Layout

| Path | Purpose |
|---|---|
| `ingestion/` | OpenSky Network poller (real data) + synthetic radar-contact generator |
| `fusion/` | Per-track state estimation (Kalman/alpha-beta filter), gating, association, conflict resolution |
| `anomaly/` | Rule-based + statistical detectors, test harness for injected anomalies |
| `api/` | Serves the fused picture and anomaly feed to the frontend |
| `web/` | Map-based visualization and replay UI |
| `docs/` | Problem statement, architecture, and design-decision writeups |

## Status

- [x] Week 1 — foundations (this scaffold, live data proof, design doc)
- [x] Weeks 2–4 — ingestion pipeline
- [x] Weeks 5–9 — fusion & entity resolution
- [ ] Weeks 10–11 — anomaly detection
- [ ] Weeks 12–13 — visualization & demo UI
- [ ] Weeks 14–15 — hardening, benchmarks, writeup
- [ ] Week 16 — buffer & presentation

## Running it

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
createdb dark_track                     # local Postgres, no auth
python3 ingestion/db.py                  # creates the raw_ticks table

python3 ingestion/opensky_poll.py        # real ADS-B feed -> raw_ticks
# optional: watch military types over the Gulf (see ingestion/README.md)
python3 ingestion/opensky_poll.py --region gulf --aircraft-db data/raw/aircraft-database.csv
python3 ingestion/radar_sim.py           # synthetic radar feed, derived from the above

python3 fusion/db.py                     # creates fused_tracks / fused_track_events
python3 fusion/engine.py                 # Kalman filter + gating + association -> fused picture
```

See [`ingestion/README.md`](ingestion/README.md) and
[`fusion/README.md`](fusion/README.md) for the full set of scripts and
tuning flags.

## License

MIT — see [`LICENSE`](LICENSE).
