#!/usr/bin/env python3
"""
opensky_poll.py — Dark Track / ingestion: proof-of-life script for the
OpenSky Network live ADS-B feed.

Polls OpenSky's public `/states/all` endpoint for a bounding box and prints
callsign, lat/lon, altitude, and velocity for every aircraft currently
inside it, once every POLL_SECONDS. This is the "week 1" task from the
roadmap — nothing else in the project matters until this runs cleanly.

No API key required at this usage level — OpenSky allows anonymous
requests at a lower rate limit (roughly one request per 10s), which is
exactly the cadence this script uses.
"""

import sys
import time
from datetime import datetime, timezone

import requests

# ---- Configure your bounding box here ----
# Default below is New York City metro. To use your own region: open
# Google Maps, right-click the SW corner of the area you want and copy the
# lat/lon, then do the same for the NE corner.
#   LAMIN, LOMIN = south-west corner (lat, lon)
#   LAMAX, LOMAX = north-east corner (lat, lon)
LAMIN, LAMAX = 40.40, 41.10     # latitude:  south, north
LOMIN, LOMAX = -74.60, -73.30   # longitude: west, east

POLL_SECONDS = 10
OPENSKY_URL = "https://opensky-network.org/api/states/all"

# OpenSky state vector field indices (from the public API docs) — named
# here so the rest of the script doesn't have unexplained magic numbers.
IDX_CALLSIGN = 1
IDX_LON = 5
IDX_LAT = 6
IDX_BARO_ALT = 7
IDX_VELOCITY = 9


def fetch_states():
    params = {"lamin": LAMIN, "lomin": LOMIN, "lamax": LAMAX, "lomax": LOMAX}
    resp = requests.get(OPENSKY_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def print_states(data):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    states = data.get("states") or []
    if not states:
        print(f"[{ts}] no aircraft currently in the bounding box")
        return
    print(f"[{ts}] {len(states)} aircraft:")
    for s in states:
        callsign = (s[IDX_CALLSIGN] or "").strip() or "?"
        lat, lon = s[IDX_LAT], s[IDX_LON]
        alt, velocity = s[IDX_BARO_ALT], s[IDX_VELOCITY]
        alt_str = f"{alt:.0f}m" if alt is not None else "n/a"
        vel_str = f"{velocity:.0f}m/s" if velocity is not None else "n/a"
        print(f"   {callsign:<10} lat={lat} lon={lon} alt={alt_str} vel={vel_str}")


def main():
    print(
        f"Polling OpenSky every {POLL_SECONDS}s for box "
        f"lat[{LAMIN},{LAMAX}] lon[{LOMIN},{LOMAX}]. Ctrl+C to stop.\n"
    )
    while True:
        try:
            data = fetch_states()
            print_states(data)
        except requests.exceptions.RequestException as e:
            print(f"request failed: {e}", file=sys.stderr)

        try:
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("\nstopped.")
            break


if __name__ == "__main__":
    main()
