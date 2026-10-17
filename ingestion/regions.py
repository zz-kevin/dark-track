"""
regions.py — Dark Track / ingestion: the bounding boxes the poller
watches, and splitting stored ticks back out by region.

Each region is fused on its own flat-earth reference (fusion/geo.py). Two
pollers running at once put both regions in the same raw_ticks window --
e.g. 2026-10-10 02:30-02:45 UTC holds ~1,600 NYC *and* ~1,100 Gulf ticks
-- and a window replayed as one centers the projection between New York
and Qatar, so every replay/eval tool filters to one region first (pick()).
"""

# (lamin, lamax, lomin, lomax): south, north, west, east. To add your own:
# open Google Maps, right-click the SW corner of the area you want and copy
# the lat/lon, then do the same for the NE corner.
REGIONS = {
    "nyc": (40.40, 41.10, -74.60, -73.30),   # New York City metro
    # Persian Gulf + Strait of Hormuz -- where most of the watched types
    # operate. Larger box, so it costs more OpenSky API credits per poll.
    "gulf": (23.00, 30.50, 47.50, 57.50),
}

# Radar contacts are dead-reckoned + jittered off ADS-B fixes, so they can
# land just outside the box the fix came from.
MARGIN_DEG = 0.5


def region_of(lat, lon):
    for name, (lamin, lamax, lomin, lomax) in REGIONS.items():
        if (lamin - MARGIN_DEG <= lat <= lamax + MARGIN_DEG
                and lomin - MARGIN_DEG <= lon <= lomax + MARGIN_DEG):
            return name
    return None


def pick(ticks, region=None):
    """`ticks` limited to one region. With region=None, the window must
    hold only one region's traffic; otherwise raises ValueError naming the
    regions present, so the caller can ask for one."""
    by_region = {}
    for t in ticks:
        by_region.setdefault(region_of(t["lat"], t["lon"]), []).append(t)
    if region is not None:
        if region not in REGIONS:
            raise ValueError(f"unknown region {region!r} (known: {', '.join(REGIONS)})")
        return by_region.get(region, [])
    present = {r: len(v) for r, v in by_region.items()}
    if len(present) > 1:
        counts = ", ".join(f"{r or 'outside any region'}: {n} ticks" for r, n in present.items())
        raise ValueError(f"window mixes regions ({counts}) -- pick one with --region")
    return ticks
