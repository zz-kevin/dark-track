# api/

Thin, read-only serving layer between `fusion/`+`anomaly/` and `web/`.
Deliberately not the interesting part of this project: stdlib
`http.server`, no new dependencies, and it serves `web/` from the same
origin so there's no CORS to configure.

```
python3 server.py              # http://localhost:8000
../run.sh api --port 8080      # same, from anywhere
```

| Endpoint | Returns |
|---|---|
| `GET /api/status` | live track count, last tick times, alerts in the last hour |
| `GET /api/tracks` | the fused picture (`fused_tracks`), each with its open anomalies |
| `GET /api/tracks/<id>/history` | every tick that updated a track: raw position, source, residual |
| `GET /api/alerts?limit=50` | latest anomaly episodes, newest first |
| `GET /api/windows` | stored 15-minute windows per region worth replaying |
| `GET /api/candidates?start=&end=&region=` | callsigns an anomaly can be injected on |
| `GET /api/replay?start=&end=&region=[&step=6][&inject=went_dark\|kinematic&target=CALLSIGN]` | the window re-run through fusion + both detectors in memory, sampled every `step` seconds, plus every alert |

Replays don't touch the database: `replay.py` runs `fusion/simulate.py`
and `anomaly/detectors.py` directly (~1 s for 15 minutes of Gulf
traffic), optionally after `anomaly/inject.py` -- the same injection the
precision/recall harness scores. The last 8 are cached in memory.

## Files

| File | Purpose |
|---|---|
| `server.py` | HTTP routing, JSON, static files from `web/` |
| `db.py` | Read-only queries over `fused_tracks`, `fused_track_events`, `anomaly_events`, `raw_ticks` |
| `replay.py` | In-memory replay of a stored window into map frames + alerts |
