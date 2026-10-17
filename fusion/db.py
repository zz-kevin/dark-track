"""
db.py — Dark Track / fusion: storage helper.

Reads raw_ticks (written by ingestion/, shared table, same DATABASE_URL)
and writes the fused picture out to fused_tracks / fused_track_events
(schema in schema.sql -- run init_schema() once before engine.py).

latest_id()/ticks_since() here mirror ingestion/db.py's functions of the
same name -- fusion is a separate consumer reading the same raw_ticks
table, so a small amount of duplicated query code is kept instead of
importing across the ingestion/fusion boundary; each module stays
runnable on its own.
"""

import os
from contextlib import contextmanager
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///dark_track")
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA_PATH.read_text())


def _bbox_sql(bbox):
    """(sql, params) restricting raw_ticks to a (lamin, lamax, lomin, lomax)
    box, or nothing for bbox=None."""
    if bbox is None:
        return "", ()
    lamin, lamax, lomin, lomax = bbox
    return " AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s", (lamin, lamax, lomin, lomax)


def recent_center(source="adsb", n=500, bbox=None):
    """Mean lat/lon of the latest n ticks, or None if there are none --
    used as geo.py's flat-earth reference point."""
    where, params = _bbox_sql(bbox)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT AVG(lat), AVG(lon) FROM (
                SELECT lat, lon FROM raw_ticks WHERE source = %s{where}
                ORDER BY id DESC LIMIT %s
            ) recent
            """,
            (source, *params, n),
        )
        lat, lon = cur.fetchone()
        return None if lat is None else (lat, lon)


def recent_points(source="adsb", n=500):
    """(lat, lon) of the latest n ticks -- to check which regions are live."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT lat, lon FROM raw_ticks WHERE source = %s ORDER BY id DESC LIMIT %s",
            (source, n),
        )
        return cur.fetchall()


def next_track_id():
    """One past the highest track_id ever used (in either table), so a
    restarted engine never reuses an id that fused_track_events already
    attributes to a different aircraft from an earlier run."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT GREATEST(
                (SELECT COALESCE(MAX(track_id), 0) FROM fused_tracks),
                (SELECT COALESCE(MAX(track_id), 0) FROM fused_track_events)
            ) + 1
            """
        )
        return cur.fetchone()[0]


def clear_fused_tracks():
    """Drop the previous run's "current picture". Its tracks lived only in
    that process's memory, so nothing will ever update or retire them --
    left in place they'd sit in fused_tracks forever. fused_track_events
    (the history) is kept."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM fused_tracks")
        return cur.rowcount


def latest_raw_id(source):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(id), 0) FROM raw_ticks WHERE source = %s",
            (source,),
        )
        return cur.fetchone()[0]


def raw_ticks_since(source, since_id, bbox=None):
    where, params = _bbox_sql(bbox)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, external_id, lat, lon, observed_at, aircraft_type,
                   aircraft_variant, velocity_mps, heading_deg, altitude_m
            FROM raw_ticks
            WHERE source = %s AND id > %s{where}
            ORDER BY id
            """,
            (source, since_id, *params),
        )
        cols = ["id", "external_id", "lat", "lon", "observed_at",
                "aircraft_type", "aircraft_variant", "velocity_mps",
                "heading_deg", "altitude_m"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


TRACK_COLS = ["track_id", "external_id", "lat", "lon", "speed_mps", "heading_deg",
              "confidence", "adsb_hits", "radar_hits", "last_adsb_at",
              "last_radar_at", "created_at", "updated_at", "aircraft_type",
              "aircraft_variant", "last_adsb_alt_m"]
EVENT_COLS = ["track_id", "source", "raw_tick_id", "observed_at", "residual_m",
              "innovation_std_m", "created_new_track"]


def write_cycle(events, track_rows, retired_ids):
    """Everything one fusion cycle produces, in one transaction: append
    the event log, upsert touched tracks, delete retired ones."""
    with get_conn() as conn, conn.cursor() as cur:
        if events:
            execute_values(
                cur,
                f"INSERT INTO fused_track_events ({', '.join(EVENT_COLS)}) VALUES %s",
                [tuple(e[c] for c in EVENT_COLS) for e in events],
            )
        if track_rows:
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in TRACK_COLS
                                if c not in ("track_id", "created_at"))
            execute_values(
                cur,
                f"INSERT INTO fused_tracks ({', '.join(TRACK_COLS)}) VALUES %s "
                f"ON CONFLICT (track_id) DO UPDATE SET {updates}",
                [tuple(r[c] for c in TRACK_COLS) for r in track_rows],
            )
        if retired_ids:
            cur.execute("DELETE FROM fused_tracks WHERE track_id = ANY(%s)",
                        (list(retired_ids),))


def adsb_update_interval(window_min=15):
    """Median seconds between consecutive ADS-B ticks of the same aircraft
    over the last window_min minutes -- i.e. the poller's effective
    cadence, which fusion/anomaly thresholds have to scale with. None if
    there isn't enough data yet. Gaps over 5 min (aircraft left and came
    back) are ignored."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY gap)
            FROM (
                SELECT EXTRACT(EPOCH FROM observed_at - LAG(observed_at)
                       OVER (PARTITION BY icao24 ORDER BY observed_at)) AS gap
                FROM raw_ticks
                WHERE source = 'adsb' AND icao24 IS NOT NULL
                  AND observed_at > now() - make_interval(mins => %s)
            ) g
            WHERE gap > 0 AND gap < 300
            """,
            (window_min,),
        )
        v = cur.fetchone()[0]
        return float(v) if v is not None else None


if __name__ == "__main__":
    init_schema()
    print(f"schema applied to {DATABASE_URL}")
