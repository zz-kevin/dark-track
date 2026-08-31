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

## First task (week 1)

A script that polls OpenSky for a bounding box and prints callsign/lat/lon/
altitude every ~10s. Nothing downstream matters until this works.
