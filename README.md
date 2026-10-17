# Dark Track

Real-time fusion of two independent, noisy sensor feeds — real ADS-B aircraft
data and a simulated non-cooperative radar contact — into a single tracked
picture, with anomaly detection for aircraft that behave like they're trying
not to be seen (transponder drops while the radar-like contact keeps flying
the same profile).

Solo capstone project. Status: **weeks 12–13 — visualization & demo UI done; next is hardening.**

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
| `docs/` | Problem statement, architecture, design-decision writeups, and a 10-minute demo script (`DEMO.md`) |
| `tests/` | Unit + end-to-end tests on synthetic traffic, no database needed (`./run.sh test`; CI runs them on every push) |

## Status

- [x] Week 1 — foundations (this scaffold, live data proof, design doc)
- [x] Weeks 2–4 — ingestion pipeline
- [x] Weeks 5–9 — fusion & entity resolution
- [x] Weeks 10–11 — anomaly detection (validation scores in [`anomaly/README.md`](anomaly/README.md))
- [x] Weeks 12–13 — visualization & demo UI
- [ ] Weeks 14–15 — hardening, benchmarks, writeup (benchmarks done: see below)
- [ ] Week 16 — buffer & presentation

Against the success criteria in [`docs/PROBLEM.md`](docs/PROBLEM.md):

| Criterion | Target | Measured |
|---|---|---|
| Fusion accuracy | 90% of aircraft | Gulf 96.7–100%; NYC 82.8% all traffic, 100% airborne (daytime validation capture) |
| Went-dark false alarms | < 5%, held-out | 0.0% (0–2% across all 19 scored windows) |
| Went-dark detection | within silence + 1 interval | 94.2–98.3% of injected events |
| Throughput | 150 tracks, < 500 ms | 100% under 500 ms at ~510 tracks (max 315 ms) |

All from validation windows nothing was tuned on: three Gulf windows
and a fresh daytime NYC capture. NYC all-traffic accuracy is the one
miss -- taxiing aircraft closer together than the radar can resolve.

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
python3 anomaly/db.py                    # creates anomaly_events
python3 anomaly/engine.py                # went-dark + kinematic detectors -> alerts

python3 api/server.py                    # web UI at http://localhost:8000 (live + replay)
```

Or `./run.sh <poll|radar|fusion|anomaly|api>` from anywhere, one per
terminal; `./run.sh status` shows what's running. If two pollers run at
once (e.g. NYC and Gulf), run one fusion engine per region with
`--region`.

The UI works without the live pipeline: **Replay** re-runs fusion and
anomaly detection over any stored 15-minute window, and can inject a
went-dark or a 2g maneuver on a chosen aircraft first, which is the
quickest demo.

`./run.sh test` runs the test suite (Kalman filter, gating and
association, detectors, injection, radar ground truth, an end-to-end
replay, and a database outage mid-run) in under a second.

The fusion and anomaly engines ride out a Postgres restart: they keep
their tracks in memory, retry with backoff, and write what they fused
in the meantime once the database is back.

Evaluating it (all replay a stored window; add `--region` if both pollers
ran during it):

```
./run.sh harness sweep went_dark <start> <end>   # anomaly precision/recall
./run.sh accuracy <start> <end>                  # fusion association accuracy
./run.sh bench <start> <end> --copies 4          # throughput / latency
```

See each module's README for the full set of scripts and tuning flags.

## License

MIT — see [`LICENSE`](LICENSE).
