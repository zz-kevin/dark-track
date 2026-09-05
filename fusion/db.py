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
            SELECT id, external_id, lat, lon, observed_at
            FROM raw_ticks
            WHERE source = %s AND id > %s
            ORDER BY id
            """,
            (source, since_id),
        )
        cols = ["id", "external_id", "lat", "lon", "observed_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def upsert_fused_track(track):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fused_tracks
                (track_id, external_id, lat, lon, speed_mps, heading_deg,
                 confidence, adsb_hits, radar_hits, last_adsb_at,
                 last_radar_at, created_at, updated_at)
            VALUES
                (%(track_id)s, %(external_id)s, %(lat)s, %(lon)s,
                 %(speed_mps)s, %(heading_deg)s, %(confidence)s,
                 %(adsb_hits)s, %(radar_hits)s, %(last_adsb_at)s,
                 %(last_radar_at)s, %(created_at)s, %(updated_at)s)
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
                updated_at = EXCLUDED.updated_at
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
                 created_new_track)
            VALUES (%(track_id)s, %(source)s, %(raw_tick_id)s,
                    %(observed_at)s, %(residual_m)s, %(created_new_track)s)
            """,
            events,
        )


if __name__ == "__main__":
    init_schema()
    print(f"schema applied to {DATABASE_URL}")
