"""
radar_model.py — Dark Track / ingestion: a radar that behaves like one.

The original radar_sim adds 150 m of Gaussian noise to every ADS-B fix and
drops 10% at random -- the same accuracy everywhere, every aircraft seen,
nothing seen that isn't there. This model puts a radar at a site and gives
it the properties that make fusion hard:

  - Polar measurement error: range is measured well (tens of metres),
    bearing to a fraction of a degree, so cross-range error grows with
    distance -- ~50 m at 20 km, ~500 m at 200 km. Height (a 3D radar's
    elevation angle) degrades the same way.
  - Detection probability falls off towards maximum range.
  - The radar horizon: the Earth's curvature hides low aircraft far away.
  - Clutter: false contacts every scan, uniformly over the coverage area.

Each contact carries its own expected accuracy (`noise_std_m`,
`altitude_std_m`) the way a real plot does, so fusion can trust a close
contact more than a distant one. Clutter carries truth_tick_id = CLUTTER.

The sites are notional -- placed for coverage of each capture region, not
real installations. Contacts are still derived from ADS-B fixes (one look
per fix, the true position known exactly), so an aircraft that never
transmitted is still not in the data: see anomaly/inject.py for the
never-identified scenario, which removes a target's ADS-B entirely.
"""

import math
import random
from dataclasses import dataclass
from datetime import timedelta

METERS_PER_DEG_LAT = 111320.0
EARTH_RADIUS_M = 6371000.0
K_REFRACTION = 4.0 / 3.0  # standard atmosphere: radio horizon ~15% past optical
CLUTTER = -1              # truth_tick_id of a false contact


@dataclass(frozen=True)
class RadarSite:
    name: str
    lat: float
    lon: float
    antenna_m: float          # antenna height above ground/sea
    max_range_m: float
    range_std_m: float = 50.0
    azimuth_std_deg: float = 0.15
    elevation_std_deg: float = 0.25
    min_height_std_m: float = 150.0
    pd_near: float = 0.95     # detection probability well inside coverage
    pd_half_frac: float = 0.85  # range (fraction of max) where Pd has halved
    clutter_per_min: float = 12.0  # false contacts per minute, whole coverage
    latency_min_s: float = 1.0
    latency_max_s: float = 4.0


# Notional sites: placed to cover each capture region (ingestion/regions.py).
SITES = {
    # Terminal-area radar near the NYC airports: ~60 nmi.
    "nyc": RadarSite("NYC terminal radar (notional)", 40.70, -73.85, 30.0, 111_000.0),
    # Long-range surveillance radar on the Gulf coast: ~200 nmi. Coverage
    # stops short of the box's far corners, as one radar's would.
    "gulf": RadarSite("Gulf long-range radar (notional)", 25.90, 51.60, 60.0, 370_000.0),
}


def _enu(site, lat, lon):
    """(east, north) metres from the site -- flat-earth, fine at radar ranges
    for placing errors (the errors, not the ranges, are what matter here)."""
    e = (lon - site.lon) * METERS_PER_DEG_LAT * math.cos(math.radians(site.lat))
    n = (lat - site.lat) * METERS_PER_DEG_LAT
    return e, n


def _latlon(site, e, n):
    return (site.lat + n / METERS_PER_DEG_LAT,
            site.lon + e / (METERS_PER_DEG_LAT * math.cos(math.radians(site.lat))))


def horizon_m(site, altitude_m):
    """Radio horizon between the antenna and a target at altitude_m."""
    k_re = K_REFRACTION * EARTH_RADIUS_M
    h_t = max(altitude_m or 0.0, 0.0)
    return math.sqrt(2 * k_re * site.antenna_m) + math.sqrt(2 * k_re * h_t)


def detection_probability(site, range_m, altitude_m):
    """Pd for a target at range_m and altitude_m (None = on the ground)."""
    if range_m > site.max_range_m or range_m > horizon_m(site, altitude_m):
        return 0.0
    r_half = site.pd_half_frac * site.max_range_m
    return site.pd_near / (1.0 + (range_m / r_half) ** 8)


def measurement_std(site, range_m):
    """(horizontal std per axis, height std), metres, at range_m. The
    horizontal figure is the isotropic equivalent of the range/cross-range
    errors: what fusion uses as the contact's R."""
    cross = range_m * math.radians(site.azimuth_std_deg)
    horiz = math.sqrt((site.range_std_m ** 2 + cross ** 2) / 2)
    height = max(site.min_height_std_m, range_m * math.radians(site.elevation_std_deg))
    return horiz, height


def advance(lat, lon, velocity, heading, seconds):
    """Dead-reckon along the reported track (same as radar_sim.advance)."""
    if velocity is None or heading is None:
        return lat, lon
    d = velocity * seconds
    return (lat + d * math.cos(math.radians(heading)) / METERS_PER_DEG_LAT,
            lon + d * math.sin(math.radians(heading))
            / (METERS_PER_DEG_LAT * math.cos(math.radians(lat))))


def contact(site, adsb_row, rng):
    """The radar's look at the aircraft behind one ADS-B fix, or None when
    the radar doesn't detect it."""
    latency = rng.uniform(site.latency_min_s, site.latency_max_s)
    lat, lon = advance(adsb_row["lat"], adsb_row["lon"],
                       adsb_row["velocity_mps"], adsb_row["heading_deg"], latency)
    e, n = _enu(site, lat, lon)
    rng_m = math.hypot(e, n)
    if rng.random() >= detection_probability(site, rng_m, adsb_row["altitude_m"]):
        return None
    # Measured in polar: range and bearing errors, independently.
    bearing = math.atan2(e, n)
    r_meas = rng_m + rng.gauss(0, site.range_std_m)
    b_meas = bearing + math.radians(rng.gauss(0, site.azimuth_std_deg))
    m_lat, m_lon = _latlon(site, r_meas * math.sin(b_meas), r_meas * math.cos(b_meas))
    horiz_std, height_std = measurement_std(site, rng_m)
    alt = adsb_row["altitude_m"]
    velocity, heading = adsb_row["velocity_mps"], adsb_row["heading_deg"]
    return {
        "source": "radar",
        "external_id": None,
        "lat": m_lat,
        "lon": m_lon,
        "altitude_m": alt + rng.gauss(0, height_std) if alt is not None else None,
        "altitude_std_m": height_std if alt is not None else None,
        "noise_std_m": horiz_std,
        # Kept for track seeding, as before; a real plot has no heading.
        "velocity_mps": velocity + rng.gauss(0, 2.0) if velocity is not None else None,
        "heading_deg": heading + rng.gauss(0, 3.0) if heading is not None else None,
        "observed_at": adsb_row["observed_at"] + timedelta(seconds=latency),
        "truth_tick_id": adsb_row["id"],
    }


def clutter(site, start, seconds, rng):
    """False contacts over `seconds` from `start`: Poisson in time, uniform
    over the coverage disk. Radar-like but belonging to no aircraft."""
    out = []
    t = 0.0
    rate = site.clutter_per_min / 60.0
    while rate > 0:
        t += rng.expovariate(rate)
        if t > seconds:
            break
        r = site.max_range_m * math.sqrt(rng.random())
        a = rng.uniform(0, 2 * math.pi)
        lat, lon = _latlon(site, r * math.sin(a), r * math.cos(a))
        horiz_std, height_std = measurement_std(site, r)
        out.append({
            "source": "radar", "external_id": None, "lat": lat, "lon": lon,
            "altitude_m": rng.uniform(0, 12000), "altitude_std_m": height_std,
            "noise_std_m": horiz_std, "velocity_mps": None, "heading_deg": None,
            "observed_at": start + timedelta(seconds=t), "truth_tick_id": CLUTTER,
        })
    return out


def site_for(ticks):
    """The site whose region this window's traffic is in (nearest site to
    the data's centre -- the regions are an ocean apart)."""
    lat = sum(t["lat"] for t in ticks) / len(ticks)
    lon = sum(t["lon"] for t in ticks) / len(ticks)
    return min(SITES.values(), key=lambda s: math.hypot(*_enu(s, lat, lon)))


def regenerate(ticks, region=None, seed=0, site=None):
    """`ticks` with their stored radar contacts replaced by this model's,
    generated from the window's ADS-B (seeded: same window, same contacts).
    For scoring stored captures with the realistic radar; generated rows get
    negative ids so they can't collide with real ones. Call this BEFORE
    injecting an anomaly -- a went-dark injection removes ADS-B, and the
    radar has to have been generated from the full picture."""
    if not ticks:
        return []
    site = site or (SITES[region] if region else site_for(ticks))
    rng = random.Random(seed)
    adsb = sorted((t for t in ticks if t["source"] == "adsb"), key=lambda t: t["observed_at"])
    radar = [c for c in (contact(site, a, rng) for a in adsb) if c is not None]
    if adsb:
        start, end = adsb[0]["observed_at"], adsb[-1]["observed_at"]
        radar += clutter(site, start, (end - start).total_seconds(), rng)
    for i, r in enumerate(radar, start=1):
        r.update(id=-i, icao24=None, aircraft_type=None, aircraft_variant=None, velocity_at=None)
    return sorted(adsb + radar, key=lambda t: t["observed_at"])
