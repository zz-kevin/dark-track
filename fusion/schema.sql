-- Dark Track / fusion: fused-picture schema.
--
-- fused_tracks holds one row per currently-live fused track (upserted on
-- every update) -- this is what api/ will read for the "current picture".
-- fused_track_events is an append-only log of every tick that updated a
-- track, including the Kalman innovation (residual_m) and the filter's own
-- innovation covariance at that update (innovation_std_m, dt-aware, grows
-- with time since last update) -- this is what anomaly/ consumes for
-- kinematic-inconsistency checks (normalizing the former by the latter,
-- not by a fixed per-source R), and what lets a fusion run be
-- debugged/replayed after the fact.

CREATE TABLE IF NOT EXISTS fused_tracks (
    track_id     BIGINT PRIMARY KEY,
    external_id  TEXT,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    speed_mps    DOUBLE PRECISION,
    heading_deg  DOUBLE PRECISION,
    confidence   DOUBLE PRECISION NOT NULL,
    adsb_hits    INTEGER NOT NULL,
    radar_hits   INTEGER NOT NULL,
    last_adsb_at TIMESTAMPTZ,
    last_radar_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS fused_track_events (
    id            BIGSERIAL PRIMARY KEY,
    track_id      BIGINT NOT NULL,
    source        TEXT NOT NULL CHECK (source IN ('adsb', 'radar')),
    raw_tick_id   BIGINT NOT NULL,
    observed_at   TIMESTAMPTZ NOT NULL,
    residual_m    DOUBLE PRECISION NOT NULL,
    innovation_std_m DOUBLE PRECISION NOT NULL,
    created_new_track BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS fused_track_events_track_idx
    ON fused_track_events (track_id, observed_at);

-- Watched aircraft type (ingestion/aircraft_types.py), inherited from the
-- track's identified ADS-B ticks. Kept after ADS-B goes quiet, so a
-- radar-only track still says what it was last identified as.
ALTER TABLE fused_tracks ADD COLUMN IF NOT EXISTS aircraft_type TEXT;
ALTER TABLE fused_tracks ADD COLUMN IF NOT EXISTS aircraft_variant TEXT;
