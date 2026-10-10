"""
db.py — Dark Track / anomaly: storage helper.

Reads the fused picture (fused_tracks / fused_track_events, written by
fusion/, same DATABASE_URL) and writes detected anomalies out to
anomaly_events (schema in schema.sql -- run init_schema() once before
engine.py). Mirrors fusion/db.py's own reasoning for reading another
module's tables directly instead of importing across the boundary: each
module stays runnable on its own against just its env var, at the cost of
a small amount of duplicated query code.
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


def fetch_tracks():
    """Current fused picture -- one row per live track. What went_dark()
    in detectors.py evaluates each poll."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT track_id, external_id, adsb_hits, radar_hits,
                   last_adsb_at, last_radar_at, updated_at
            FROM fused_tracks
            """
        )
        cols = ["track_id", "external_id", "adsb_hits", "radar_hits",
                "last_adsb_at", "last_radar_at", "updated_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def latest_event_id():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM fused_track_events")
        return cur.fetchone()[0]


def events_since(since_id):
    """fused_track_events rows with id > since_id, ordered by id -- the
    Kalman innovations kinematic_inconsistency() in detectors.py consumes.
    innovation_std_m is the filter's own dt-aware innovation covariance
    (sqrt(S) at that update), what residual_m gets normalized by -- not a
    fixed per-source R, which doesn't account for the process noise
    accrued since the track's last update."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, track_id, source, observed_at, residual_m, innovation_std_m
            FROM fused_track_events
            WHERE id > %s
            ORDER BY id
            """,
            (since_id,),
        )
        cols = ["id", "track_id", "source", "observed_at", "residual_m", "innovation_std_m"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def open_track_ids(kind):
    """track_ids with a currently-unresolved episode of `kind` -- used at
    engine.py startup so restart doesn't forget which episodes were open."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT track_id FROM anomaly_events WHERE kind = %s AND resolved_at IS NULL",
            (kind,),
        )
        return {row[0] for row in cur.fetchall()}


def insert_anomaly_event(track_id, external_id, kind, detected_at, metric, source):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO anomaly_events
                (track_id, external_id, kind, detected_at, metric, source)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (track_id, kind) WHERE resolved_at IS NULL DO NOTHING
            """,
            (track_id, external_id, kind, detected_at, metric, source),
        )


def resolve_episode(track_id, kind, resolved_at):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE anomaly_events SET resolved_at = %s
            WHERE track_id = %s AND kind = %s AND resolved_at IS NULL
            """,
            (resolved_at, track_id, kind),
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
