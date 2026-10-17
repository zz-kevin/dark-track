# web/

Map-based UI over `api/`: plain HTML/CSS/JS, Leaflet from cdnjs, no
build step. Served by `api/server.py` -- open http://localhost:8000.

- **Live** -- the current fused picture, refreshed every 2s. Arrows point
  along each track's heading, colored by state: ADS-B-corroborated
  (cyan), radar-only (amber), went dark (red, pulsing), ADS-B quiet
  (grey); white outline = watched military type. Click a track for its
  details and a trail of the raw ADS-B fixes and radar contacts fused
  into it. Alerts list on the right.
- **Replay** -- pick a stored 15-minute window, optionally inject a
  went-dark or a 2g maneuver on one aircraft, and play the fused picture
  back (5-60x) with alerts appearing as they fire and tick marks on the
  scrubber. With an injection, playback starts 90 s before it and
  selects the target. Click an alert to jump to it.

The live view's "ADS-B quiet" colour is a display heuristic (no ADS-B in
the last 90 s of a track's updates); the went-dark *alert* comes from
`anomaly/` with cadence-scaled thresholds.

## Files

| File | Purpose |
|---|---|
| `index.html` | Layout |
| `style.css` | Dark theme, track markers, phone layout under 760 px |
| `app.js` | Map, live polling, replay player, inspector, alerts |
