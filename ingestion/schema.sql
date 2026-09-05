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
