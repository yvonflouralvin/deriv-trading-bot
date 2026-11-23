-- Schéma de la plateforme (idempotent : rejoué à chaque démarrage de l'API
-- et du collecteur, sous verrou consultatif).

DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'TimescaleDB indisponible : tables PostgreSQL classiques';
END $$;

-- Catalogue des actifs proposés par Deriv (rafraîchi par le collecteur)
CREATE TABLE IF NOT EXISTS symbols (
    symbol            text PRIMARY KEY,
    display_name      text NOT NULL,
    market            text,
    market_display    text,
    submarket_display text,
    pip               double precision,
    exchange_open     boolean,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Actifs que le collecteur doit suivre (piloté depuis le Monitor)
CREATE TABLE IF NOT EXISTS watchlist (
    symbol        text PRIMARY KEY,
    enabled       boolean NOT NULL DEFAULT true,
    backfill_days integer NOT NULL DEFAULT 7 CHECK (backfill_days BETWEEN 1 AND 365),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ticks (
    symbol text             NOT NULL,
    ts     timestamptz      NOT NULL,
    quote  double precision NOT NULL,
    PRIMARY KEY (symbol, ts)
);

-- tf = durée de la bougie en secondes (60, 300, 900, 3600, 14400, 86400)
CREATE TABLE IF NOT EXISTS candles (
    symbol text             NOT NULL,
    tf     integer          NOT NULL,
    ts     timestamptz      NOT NULL,
    open   double precision NOT NULL,
    high   double precision NOT NULL,
    low    double precision NOT NULL,
    close  double precision NOT NULL,
    PRIMARY KEY (symbol, tf, ts)
);

-- Dessins du Monitor (export JSON de lightweight-charts-drawing), par actif
CREATE TABLE IF NOT EXISTS drawings (
    symbol     text PRIMARY KEY,
    data       jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Battement de cœur des services (affiché dans le Monitor)
CREATE TABLE IF NOT EXISTS service_status (
    name      text PRIMARY KEY,
    last_seen timestamptz NOT NULL DEFAULT now(),
    info      jsonb NOT NULL DEFAULT '{}'::jsonb
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable('ticks', 'ts', if_not_exists => true, migrate_data => true);
        PERFORM create_hypertable('candles', 'ts', if_not_exists => true, migrate_data => true,
                                  chunk_time_interval => INTERVAL '30 days');
        -- les ticks bruts sont volumineux : on garde 30 jours
        PERFORM add_retention_policy('ticks', INTERVAL '30 days', if_not_exists => true);
    END IF;
END $$;

-- Prévenir le collecteur dès que la watchlist change
CREATE OR REPLACE FUNCTION notify_watchlist() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('watchlist', 'changed');
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS watchlist_changed ON watchlist;
CREATE TRIGGER watchlist_changed
    AFTER INSERT OR UPDATE OR DELETE ON watchlist
    FOR EACH STATEMENT EXECUTE FUNCTION notify_watchlist();
