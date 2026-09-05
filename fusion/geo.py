"""
geo.py — Dark Track / fusion: lat/lon <-> local flat-earth ENU meters.

The Kalman filter in kalman.py works in a local east/north meters frame,
not lat/lon degrees, so distances and velocities are plain Euclidean math
instead of spherical geometry. REF_LAT/REF_LON is the center of the
bounding box configured in ingestion/opensky_poll.py -- if you change that
bounding box for a different region, update this to match, or tracks far
from the reference point will pick up small flat-earth distortion.

This is a small-angle approximation valid at metro-region scale (tens of
km). It would need a proper projection (or per-track local origins) to
hold up over a whole country -- not a concern at this project's scope.
"""

import math

REF_LAT = 40.75    # center of ingestion's default NYC bounding box
REF_LON = -73.95
METERS_PER_DEG_LAT = 111320.0


def to_local(lat, lon):
    """lat/lon degrees -> (x, y) meters east/north of REF_LAT/REF_LON."""
    y = (lat - REF_LAT) * METERS_PER_DEG_LAT
    x = (lon - REF_LON) * METERS_PER_DEG_LAT * math.cos(math.radians(REF_LAT))
    return x, y


def to_latlon(x, y):
    """(x, y) meters east/north of the reference -> lat/lon degrees."""
    lat = REF_LAT + y / METERS_PER_DEG_LAT
    lon = REF_LON + x / (METERS_PER_DEG_LAT * math.cos(math.radians(REF_LAT)))
    return lat, lon
