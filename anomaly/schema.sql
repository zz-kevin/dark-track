-- Dark Track / anomaly: detected-anomaly log.
--
-- One row per detected *episode*, edge-triggered: inserted once when a
-- track transitions into an anomalous state, not once per poll cycle for
-- as long as it stays that way. resolved_at is filled in when the track
-- exits the anomalous state (ADS-B resumes, residuals return to normal, or
-- the track is retired), so an episode's duration is recoverable for the
-- precision/recall/latency scoring in anomaly/harness.py.

CREATE TABLE IF NOT EXISTS anomaly_events (
    id            BIGSERIAL PRIMARY KEY,
    track_id      BIGINT NOT NULL,
    external_id   TEXT,
    kind          TEXT NOT NULL CHECK (kind IN ('went_dark', 'kinematic_inconsistency')),
    detected_at   TIMESTAMPTZ NOT NULL,
    resolved_at   TIMESTAMPTZ,
    -- Detector-specific number that tripped the alert: went_dark stores the
    -- ADS-B silence gap in seconds; kinematic_inconsistency stores the
    -- normalized residual (residual_m / that source's assumed R_std).
    metric        DOUBLE PRECISION NOT NULL,
    -- went_dark: the source still corroborating the track ('radar').
    -- kinematic_inconsistency: the source whose residual tripped it.
    source        TEXT NOT NULL CHECK (source IN ('adsb', 'radar'))
);

CREATE INDEX IF NOT EXISTS anomaly_events_track_idx
    ON anomaly_events (track_id, detected_at);

-- At most one *open* (unresolved) episode of a given kind per track --
-- enforces the edge-triggered dedup at the DB level as a backstop to
-- engine.py's in-memory bookkeeping (which is what's actually relied on;
-- see anomaly/README.md known limitations).
CREATE UNIQUE INDEX IF NOT EXISTS anomaly_events_open_episode_idx
    ON anomaly_events (track_id, kind)
    WHERE resolved_at IS NULL;
