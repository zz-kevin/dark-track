-- Dark Track / ingestion: shared raw-track schema.
--
-- Both producers (OpenSky poller, synthetic radar generator) write into this
-- single table so downstream code doesn't care which source a tick came
-- from until fusion needs to. See docs/ARCHITECTURE.md for the data flow.

CREATE TABLE IF NOT EXISTS raw_ticks (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT NOT NULL CHECK (source IN ('adsb', 'radar')),
    -- Callsign for adsb ticks. Always NULL for radar ticks -- the whole
    -- point of the synthetic source is that it carries no ID, which is what
    -- forces real gating/association logic downstream.
    external_id  TEXT,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    altitude_m   DOUBLE PRECISION,
    velocity_mps DOUBLE PRECISION,
    heading_deg  DOUBLE PRECISION,
    -- When the position was actually true/observed (radar ticks carry the
    -- source aircraft's real timestamp plus simulated latency jitter, so
    -- replay reproduces the same relative timing between sources).
    observed_at  TIMESTAMPTZ NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS raw_ticks_source_observed_idx
    ON raw_ticks (source, observed_at);

CREATE INDEX IF NOT EXISTS raw_ticks_external_id_idx
    ON raw_ticks (external_id)
    WHERE external_id IS NOT NULL;

-- Airframe identity for adsb ticks (always NULL for radar). icao24 is the
-- transponder's 24-bit address; aircraft_type/aircraft_variant come from
-- matching it against the watched-type list in aircraft_types.py (e.g.
-- 'F35' / 'C', or 'US_MIL' for the US military hex block with no type
-- match). ADD COLUMN IF NOT EXISTS so this re-applies cleanly on an
-- existing database.
ALTER TABLE raw_ticks ADD COLUMN IF NOT EXISTS icao24 TEXT;
ALTER TABLE raw_ticks ADD COLUMN IF NOT EXISTS aircraft_type TEXT;
ALTER TABLE raw_ticks ADD COLUMN IF NOT EXISTS aircraft_variant TEXT;

CREATE INDEX IF NOT EXISTS raw_ticks_aircraft_type_idx
    ON raw_ticks (aircraft_type, observed_at)
    WHERE aircraft_type IS NOT NULL;

-- One row per aircraft per position fix. Without this, two pollers running
-- at once (or a restarted one re-fetching the same state vector) store the
-- same tick twice, and fusion sees two identical updates. Inserts use
-- ON CONFLICT DO NOTHING, so a duplicate is silently skipped.
CREATE UNIQUE INDEX IF NOT EXISTS raw_ticks_adsb_dedup_idx
    ON raw_ticks (icao24, observed_at)
    WHERE source = 'adsb' AND icao24 IS NOT NULL;

-- Radar contacts only: the adsb row radar_sim.py derived this contact from.
-- Simulation ground truth for scoring (fusion/accuracy.py, anomaly/inject.py
-- via radar_truth.py) -- fusion/ never reads it, or the radar would no
-- longer be anonymous. NULL on contacts written before it was recorded;
-- radar_truth.py infers those.
ALTER TABLE raw_ticks ADD COLUMN IF NOT EXISTS truth_tick_id BIGINT;
