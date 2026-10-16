"""
db.py — Dark Track / api: read-only queries for the live picture.

Reads the tables the other modules write (raw_ticks, fused_tracks,
fused_track_events, anomaly_events) and never writes anything. Same
DATABASE_URL convention as every other module.
"""

import os
from contextlib import contextmanager

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///dark_track")


@contextmanager
def get_cursor():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def current_tracks():
    """The fused picture, with each track's open anomaly kinds. An episode
    only counts while its track is still live: anomaly/engine.py resolves
    episodes as it runs, but one stopped mid-episode leaves them open."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT t.track_id, t.external_id, t.lat, t.lon, t.speed_mps,
                   t.heading_deg, t.confidence, t.adsb_hits, t.radar_hits,
                   t.last_adsb_at, t.last_radar_at, t.updated_at,
                   t.aircraft_type, t.aircraft_variant, t.last_adsb_alt_m,
                   COALESCE(array_agg(a.kind) FILTER (WHERE a.kind IS NOT NULL),
                            '{}') AS anomalies
            FROM fused_tracks t
            LEFT JOIN anomaly_events a
                   ON a.track_id = t.track_id AND a.resolved_at IS NULL
            GROUP BY t.track_id
            """
        )
        return _rows(cur)


def track_history(track_id, limit=500):
    """Every tick that updated this track, oldest first, with the raw
    measurement position and the filter's innovation at that update."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM (
                SELECT e.observed_at, e.source, e.residual_m, e.innovation_std_m,
                       e.created_new_track, r.lat, r.lon, r.altitude_m
                FROM fused_track_events e
                JOIN raw_ticks r ON r.id = e.raw_tick_id
                WHERE e.track_id = %s
                ORDER BY e.id DESC
                LIMIT %s
            ) recent ORDER BY observed_at
            """,
            (track_id, limit),
        )
        return _rows(cur)


def recent_alerts(limit=50):
    """Latest anomaly episodes, newest first, with whether the track is
    still in the live picture."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.track_id, a.external_id, a.kind, a.detected_at,
                   a.resolved_at, a.metric, a.source,
                   (t.track_id IS NOT NULL) AS live,
                   t.aircraft_type, t.aircraft_variant
            FROM anomaly_events a
            LEFT JOIN fused_tracks t ON t.track_id = a.track_id
            ORDER BY a.detected_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return _rows(cur)


def status():
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT (SELECT count(*) FROM fused_tracks) AS tracks,
                   (SELECT max(updated_at) FROM fused_tracks) AS picture_updated_at,
                   (SELECT max(observed_at) FROM raw_ticks WHERE source = 'adsb') AS last_adsb_at,
                   (SELECT max(observed_at) FROM raw_ticks WHERE source = 'radar') AS last_radar_at,
                   (SELECT count(*) FROM anomaly_events
                     WHERE detected_at > now() - interval '1 hour') AS alerts_last_hour
            """
        )
        return _rows(cur)[0]


def capture_windows(bucket_min=15, min_ticks=1000):
    """Stored stretches of data worth replaying: bucket_min-minute buckets
    per region with both sources present, newest first. A bucket where
    two pollers ran at once is listed once per region."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT to_timestamp(floor(extract(epoch FROM observed_at) / (%s * 60)) * %s * 60)
                       AS start,
                   CASE WHEN lon < 0 THEN 'nyc' ELSE 'gulf' END AS region,
                   count(*) FILTER (WHERE source = 'adsb') AS adsb,
                   count(*) FILTER (WHERE source = 'radar') AS radar,
                   count(DISTINCT icao24) AS aircraft
            FROM raw_ticks
            GROUP BY 1, 2
            HAVING count(*) FILTER (WHERE source = 'radar') > 0 AND count(*) >= %s
            ORDER BY 1 DESC, 2
            """,
            (bucket_min, bucket_min, min_ticks),
        )
        return _rows(cur)
