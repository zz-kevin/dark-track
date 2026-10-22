# web/

Map-based UI over `api/`: plain HTML/CSS/JS, Leaflet from cdnjs, no
build step. Served by `api/server.py` -- open http://localhost:8000.

Written for someone who has never seen it: a first visit opens a short
guide with a **Show me an example** button (a US-military-flagged plane
over the Gulf switches its transponder off, and the map follows it), the
**?** button brings the guide back, every control has a tooltip, and the
labels say what happens rather than how ("A plane switches its
transponder off", not "inject went_dark"). It opens on **Live** when the
live feed is running and on **Recordings** otherwise.

- **Recordings** -- pick a stored 15-minute window, optionally simulate a
  plane going dark or swerving (2g maneuver), and play it back (5-60x,
  space bar to pause), with alerts appearing as they fire, tick marks on
  the slider, and a pop-up for each. With an event, playback starts 90 s
  before it and selects that plane. **Radar** picks the simulated radar:
  *Simple* (the stored contacts: every plane, same accuracy) or
  *Realistic* (ingestion/radar_model.py: one radar site, drawn on the map
  with a dotted ring for its range; fuzzier far away, nothing below the
  horizon, and false echoes, which show up as short-lived amber "radar
  only" tracks).
- **Live** -- the current fused picture, refreshed every 2 s, with a
  pop-up for each new alert.

Arrows point along each plane's heading, coloured by state: identified
(cyan), dark -- transponder off but radar still tracking (red, pulsing),
radar only (amber), transponder quiet (grey); white outline = watched
military type.

## Following a dark plane

- **Dark aircraft panel** -- every plane flying dark right now: how long
  it's been dark, how far it has flown since, heading and speed.
- **Follow** (panel, details, or the pop-up) -- keeps the plane centred,
  with a banner saying how long it's been dark. Moving the map yourself
  stops following.
- On the map, for a dark plane: a hollow circle where its own transponder
  last put it, a dashed line from there to where radar sees it now, a ring
  for where it could be (two standard deviations of the fused position,
  predicted to the moment shown), and for the selected or followed plane,
  where it will be in 5 minutes if it holds course.

The data behind this comes from fusion: each track's last transponder
position and position uncertainty (`fused_tracks.last_adsb_lat`,
`last_adsb_lon`, `position_std_m`; replay frame rows -- see
`api/replay.py` `row_fields`).

The "transponder quiet" colour in Live is a display heuristic (no ADS-B
in the last 90 s of a track's updates); the went-dark *alert* comes from
`anomaly/` with cadence-scaled thresholds.

## Files

| File | Purpose |
|---|---|
| `index.html` | Layout, the welcome guide |
| `style.css` | Dark theme, track markers, dark-plane overlays, phone layout under 760 px |
| `app.js` | Map, live polling, replay player, dark panel, follow mode, pop-ups, inspector |
