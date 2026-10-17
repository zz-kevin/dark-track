"""
support.py — Dark Track / tests: imports and synthetic traffic.

fusion/ modules import their siblings by bare name (`import db`,
`import geo`, ...), so fusion/ goes on sys.path first; ingestion/ and
anomaly/ each have their own db.py, so their modules are loaded by path
under distinct names. Nothing here connects to Postgres.
"""

import importlib.util
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "fusion"))

import associate  # noqa: E402,F401
import engine  # noqa: E402,F401
import geo  # noqa: E402,F401
import kalman  # noqa: E402,F401
import simulate  # noqa: E402,F401
from track import Track  # noqa: E402,F401


def load(name, relpath):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


regions = load("dt_regions", "ingestion/regions.py")
radar_truth = load("dt_radar_truth", "ingestion/radar_truth.py")
radar_sim = load("dt_radar_sim", "ingestion/radar_sim.py")
detectors = load("dt_detectors", "anomaly/detectors.py")
inject = load("dt_inject", "anomaly/inject.py")

T0 = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)
METERS_PER_DEG_LAT = 111320.0


def flight(callsign, lat0, lon0, heading_deg, speed_mps, minutes=10, interval_s=10.0,
           alt_m=9000.0, start=T0, icao24=None, first_id=1):
    """ADS-B rows for an aircraft flying straight and level."""
    rows = []
    n = int(minutes * 60 / interval_s)
    for i in range(n):
        dist = speed_mps * i * interval_s
        dn = dist * math.cos(math.radians(heading_deg))
        de = dist * math.sin(math.radians(heading_deg))
        lat = lat0 + dn / METERS_PER_DEG_LAT
        lon = lon0 + de / (METERS_PER_DEG_LAT * math.cos(math.radians(lat0)))
        at = start + timedelta(seconds=i * interval_s)
        rows.append({
            "id": first_id + i, "source": "adsb", "external_id": callsign,
            "icao24": icao24 or callsign.lower(), "lat": lat, "lon": lon,
            "altitude_m": alt_m, "velocity_mps": speed_mps, "heading_deg": heading_deg,
            "observed_at": at, "velocity_at": at, "aircraft_type": None,
            "aircraft_variant": None, "truth_tick_id": None,
        })
    return rows


def with_radar(adsb_rows, seed=0, dropout=0.0, record_truth=True, first_id=100000):
    """adsb_rows plus radar contacts derived from them exactly as
    ingestion/radar_sim.py does (150 m noise, 1-4 s latency)."""
    rng = random.Random(seed)
    out = list(adsb_rows)
    rid = first_id
    for row in adsb_rows:
        if rng.random() < dropout:
            continue
        r = radar_sim.make_radar_row(row, 150.0, 1.0, 4.0, rng)
        r.update(id=rid, icao24=None, aircraft_type=None, aircraft_variant=None,
                 velocity_at=None)
        if not record_truth:
            r["truth_tick_id"] = None
        out.append(r)
        rid += 1
    return sorted(out, key=lambda t: t["observed_at"])


def traffic(n_aircraft=4, spacing_deg=0.05, **kw):
    """n parallel aircraft ~5 km apart over the Gulf, with radar."""
    adsb = []
    for k in range(n_aircraft):
        adsb += flight(f"TST{k}", 25.0 + k * spacing_deg, 53.0, 90.0, 220.0,
                       first_id=1 + k * 1000, **kw)
    return with_radar(adsb)
