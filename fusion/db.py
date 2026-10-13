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


def recent_center(source="adsb", n=500):
    """Mean lat/lon of the latest n ticks, or None if there are none --
    used as geo.py's flat-earth reference point."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT AVG(lat), AVG(lon) FROM (
                SELECT lat, lon FROM raw_ticks WHERE source = %s
                ORDER BY id DESC LIMIT %s
            ) recent
            """,
            (source, n),
        )
        lat, lon = cur.fetchone()
        return None if lat is None else (lat, lon)


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


def raw_ticks_since(source, since_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, external_id, lat, lon, observed_at, aircraft_type,
                   aircraft_variant, velocity_mps, heading_deg, altitude_m
            FROM raw_ticks
            WHERE source = %s AND id > %s
            ORDER BY id
            """,
            (source, since_id),
        )
        cols = ["id", "external_id", "lat", "lon", "observed_at",
                "aircraft_type", "aircraft_variant", "velocity_mps",
                "heading_deg", "altitude_m"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def upsert_fused_track(track):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fused_tracks
                (track_id, external_id, lat, lon, speed_mps, heading_deg,
                 confidence, adsb_hits, radar_hits, last_adsb_at,
                 last_radar_at, created_at, updated_at, aircraft_type,
                 aircraft_variant, last_adsb_alt_m)
            VALUES
                (%(track_id)s, %(external_id)s, %(lat)s, %(lon)s,
                 %(speed_mps)s, %(heading_deg)s, %(confidence)s,
                 %(adsb_hits)s, %(radar_hits)s, %(last_adsb_at)s,
                 %(last_radar_at)s, %(created_at)s, %(updated_at)s,
                 %(aircraft_type)s, %(aircraft_variant)s, %(last_adsb_alt_m)s)
            ON CONFLICT (track_id) DO UPDATE SET
                external_id = EXCLUDED.external_id,
                lat = EXCLUDED.lat,
                lon = EXCLUDED.lon,
                speed_mps = EXCLUDED.speed_mps,
                heading_deg = EXCLUDED.heading_deg,
                confidence = EXCLUDED.confidence,
                adsb_hits = EXCLUDED.adsb_hits,
                radar_hits = EXCLUDED.radar_hits,
                last_adsb_at = EXCLUDED.last_adsb_at,
                last_radar_at = EXCLUDED.last_radar_at,
                updated_at = EXCLUDED.updated_at,
                aircraft_type = EXCLUDED.aircraft_type,
                aircraft_variant = EXCLUDED.aircraft_variant,
                last_adsb_alt_m = EXCLUDED.last_adsb_alt_m
            """,
            track,
        )


def delete_fused_track(track_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM fused_tracks WHERE track_id = %s", (track_id,))


def insert_fused_events(events):
    if not events:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO fused_track_events
                (track_id, source, raw_tick_id, observed_at, residual_m,
                 innovation_std_m, created_new_track)
            VALUES (%(track_id)s, %(source)s, %(raw_tick_id)s,
                    %(observed_at)s, %(residual_m)s, %(innovation_std_m)s,
                    %(created_new_track)s)
            """,
            events,
        )


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
