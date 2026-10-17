"""
db.py — Dark Track / ingestion: shared storage helper.

Both producers (OpenSky poller, synthetic radar generator) write into the
same `raw_ticks` table via this module, so downstream code doesn't care
which source a tick came from until fusion needs to. Schema lives in
schema.sql; run init_schema() once (or `psql -f schema.sql`) before either
producer runs.

Connection string is read from the DATABASE_URL env var, defaulting to a
local `dark_track` database with no auth -- matches a bare `createdb
dark_track` on a locally running Postgres.
"""

import os
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.errors

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


def insert_tick(
    source,
    lat,
    lon,
    observed_at,
    external_id=None,
    altitude_m=None,
    velocity_mps=None,
    heading_deg=None,
    icao24=None,
    aircraft_type=None,
    aircraft_variant=None,
):
    """Insert one raw tick. `source` is 'adsb' or 'radar'."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_ticks
                (source, external_id, lat, lon, altitude_m, velocity_mps,
                 heading_deg, observed_at, icao24, aircraft_type,
                 aircraft_variant)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (source, external_id, lat, lon, altitude_m, velocity_mps,
             heading_deg, observed_at, icao24, aircraft_type,
             aircraft_variant),
        )


def insert_ticks(rows):
    """Bulk insert. Each row is a dict with the same keys as insert_tick's
    kwargs (source, lat, lon, observed_at required; rest optional). An adsb
    row already stored for the same (icao24, observed_at) is skipped."""
    if not rows:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO raw_ticks
                (source, external_id, lat, lon, altitude_m, velocity_mps,
                 heading_deg, observed_at, icao24, aircraft_type,
                 aircraft_variant, truth_tick_id, velocity_at, altitude_std_m)
            VALUES (%(source)s, %(external_id)s, %(lat)s, %(lon)s,
                    %(altitude_m)s, %(velocity_mps)s, %(heading_deg)s,
                    %(observed_at)s, %(icao24)s, %(aircraft_type)s,
                    %(aircraft_variant)s, %(truth_tick_id)s, %(velocity_at)s,
                    %(altitude_std_m)s)
            ON CONFLICT DO NOTHING
            """,
            [
                {
                    "external_id": None,
                    "altitude_m": None,
                    "velocity_mps": None,
                    "heading_deg": None,
                    "icao24": None,
                    "aircraft_type": None,
                    "aircraft_variant": None,
                    "truth_tick_id": None,
                    "velocity_at": None,
                    "altitude_std_m": None,
                    **row,
                }
                for row in rows
            ],
        )


@contextmanager
def schema_hint():
    """Turn "column ... does not exist" into what to do about it: raw_ticks
    gains columns over time (truth_tick_id, velocity_at, altitude_std_m),
    and an older database needs `python3 ingestion/db.py` once."""
    try:
        yield
    except psycopg2.errors.UndefinedColumn as e:
        raise RuntimeError(
            f"{str(e).strip().splitlines()[0]} -- this version reads raw_ticks columns "
            f"your database doesn't have yet. Run `python3 ingestion/db.py` once "
            f"(adds them; existing rows are kept).") from None


def latest_id(source):
    """Highest raw_ticks.id for a given source, or 0 if there are none yet.
    Used by radar_sim.py to know where to resume reading adsb ticks from."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(id), 0) FROM raw_ticks WHERE source = %s",
            (source,),
        )
        return cur.fetchone()[0]


def ticks_since(source, since_id):
    """All rows for `source` with id > since_id, ordered by id."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, external_id, lat, lon, altitude_m, velocity_mps,
                   heading_deg, observed_at, icao24, aircraft_type,
                   aircraft_variant
            FROM raw_ticks
            WHERE source = %s AND id > %s
            ORDER BY id
            """,
            (source, since_id),
        )
        cols = ["id", "external_id", "lat", "lon", "altitude_m",
                "velocity_mps", "heading_deg", "observed_at", "icao24",
                "aircraft_type", "aircraft_variant"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def ticks_in_window(start, end, source=None, aircraft_types=None):
    """All rows with observed_at in [start, end], ordered by observed_at.
    Used by replay.py. Optionally filter to one source and/or a list of
    aircraft_type keys (see aircraft_types.py)."""
    query = """
        SELECT id, source, external_id, lat, lon, altitude_m, velocity_mps,
               heading_deg, observed_at, icao24, aircraft_type,
               aircraft_variant, truth_tick_id, velocity_at, altitude_std_m
        FROM raw_ticks
        WHERE observed_at BETWEEN %s AND %s
    """
    params = [start, end]
    if source is not None:
        query += " AND source = %s"
        params.append(source)
    if aircraft_types:
        query += " AND aircraft_type = ANY(%s)"
        params.append(list(aircraft_types))
    query += " ORDER BY observed_at"

    with get_conn() as conn, conn.cursor() as cur:
        with schema_hint():
            cur.execute(query, params)
        cols = ["id", "source", "external_id", "lat", "lon", "altitude_m",
                "velocity_mps", "heading_deg", "observed_at", "icao24",
                "aircraft_type", "aircraft_variant", "truth_tick_id", "velocity_at",
                "altitude_std_m"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


if __name__ == "__main__":
    init_schema()
    print(f"schema applied to {DATABASE_URL}")
