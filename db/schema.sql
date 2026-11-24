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

-- =========================================================================
-- Moteur de stratégie (service 3)
-- =========================================================================

-- Types de stratégie disponibles (publiés par le moteur au démarrage)
CREATE TABLE IF NOT EXISTS strategy_kinds (
    kind        text PRIMARY KEY,
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    params      jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{key,label,default,min,max,step}]
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Instances configurées depuis le Monitor
CREATE TABLE IF NOT EXISTS strategies (
    id                 serial PRIMARY KEY,
    name               text NOT NULL,
    kind               text NOT NULL,
    symbol             text NOT NULL,
    tf                 integer NOT NULL,
    params             jsonb NOT NULL DEFAULT '{}'::jsonb,
    mode               text NOT NULL DEFAULT 'paper' CHECK (mode IN ('paper', 'demo')),
    enabled            boolean NOT NULL DEFAULT false,
    stake              double precision NOT NULL DEFAULT 1 CHECK (stake > 0),
    duration           integer NOT NULL DEFAULT 5 CHECK (duration > 0),
    duration_unit      text NOT NULL DEFAULT 't' CHECK (duration_unit IN ('t', 's', 'm')),
    payout_ratio       double precision NOT NULL DEFAULT 0.95 CHECK (payout_ratio > 0),
    max_trades_per_day integer NOT NULL DEFAULT 20 CHECK (max_trades_per_day > 0),
    max_daily_loss     double precision NOT NULL DEFAULT 10 CHECK (max_daily_loss > 0),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS signals (
    id          bigserial PRIMARY KEY,
    strategy_id integer REFERENCES strategies(id) ON DELETE CASCADE,
    symbol      text NOT NULL,
    tf          integer NOT NULL,
    candle_ts   timestamptz NOT NULL,
    side        text NOT NULL CHECK (side IN ('CALL', 'PUT')),
    price       double precision NOT NULL,
    action      text NOT NULL,            -- traded / skipped:<raison>
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS signals_strategy_idx ON signals (strategy_id, created_at DESC);

CREATE TABLE IF NOT EXISTS trades (
    id            bigserial PRIMARY KEY,
    strategy_id   integer REFERENCES strategies(id) ON DELETE CASCADE,
    signal_id     bigint REFERENCES signals(id) ON DELETE SET NULL,
    mode          text NOT NULL CHECK (mode IN ('paper', 'demo')),
    symbol        text NOT NULL,
    side          text NOT NULL CHECK (side IN ('CALL', 'PUT')),
    stake         double precision NOT NULL,
    payout        double precision,
    duration      integer NOT NULL,
    duration_unit text NOT NULL,
    entry_time    timestamptz NOT NULL,
    entry_spot    double precision,
    exit_time     timestamptz,
    exit_spot     double precision,
    profit        double precision,
    status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'won', 'lost', 'error')),
    contract_id   text,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trades_strategy_idx ON trades (strategy_id, entry_time DESC);
CREATE INDEX IF NOT EXISTS trades_symbol_idx ON trades (symbol, entry_time DESC);
CREATE INDEX IF NOT EXISTS trades_open_idx ON trades (status) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS backtests (
    id            serial PRIMARY KEY,
    strategy_id   integer REFERENCES strategies(id) ON DELETE SET NULL,
    kind          text NOT NULL,
    symbol        text NOT NULL,
    tf            integer NOT NULL,
    params        jsonb NOT NULL DEFAULT '{}'::jsonb,
    stake         double precision NOT NULL DEFAULT 1,
    duration      integer NOT NULL DEFAULT 5,
    duration_unit text NOT NULL DEFAULT 't',
    payout_ratio  double precision NOT NULL DEFAULT 0.95,
    date_from     timestamptz NOT NULL,
    date_to       timestamptz NOT NULL,
    status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'done', 'error')),
    stats         jsonb,
    trades        jsonb,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);

CREATE OR REPLACE FUNCTION notify_strategies() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('strategies', 'changed');
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS strategies_changed ON strategies;
CREATE TRIGGER strategies_changed
    AFTER INSERT OR UPDATE OR DELETE ON strategies
    FOR EACH STATEMENT EXECUTE FUNCTION notify_strategies();

CREATE OR REPLACE FUNCTION notify_backtests() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('backtests', NEW.id::text);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS backtests_created ON backtests;
CREATE TRIGGER backtests_created
    AFTER INSERT ON backtests
    FOR EACH ROW EXECUTE FUNCTION notify_backtests();

-- =========================================================================
-- Analyse de marché : tendance multi-timeframe (étape 1)
-- =========================================================================
CREATE TABLE IF NOT EXISTS trend_states (
    symbol    text     NOT NULL,
    tf        integer  NOT NULL,
    ts        timestamptz NOT NULL,      -- début de la bougie clôturée
    state     smallint NOT NULL,         -- -1 baissier / 0 neutre / +1 haussier
    score     real     NOT NULL,
    strength  smallint NOT NULL,         -- 0..100
    adx       real,
    structure smallint,
    ema       smallint,
    di        smallint,
    version   smallint NOT NULL,
    PRIMARY KEY (symbol, tf, ts)
);

CREATE TABLE IF NOT EXISTS trend_reports (
    id          serial PRIMARY KEY,
    symbols     text[]  NOT NULL,
    tfs         integer[] NOT NULL,
    days        integer NOT NULL,
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'done', 'error')),
    result      jsonb,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE OR REPLACE FUNCTION notify_trend_reports() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('trend_reports', NEW.id::text);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trend_reports_created ON trend_reports;
CREATE TRIGGER trend_reports_created
    AFTER INSERT ON trend_reports
    FOR EACH ROW EXECUTE FUNCTION notify_trend_reports();

-- v2 : comparateur de variantes du détecteur
ALTER TABLE trend_reports ADD COLUMN IF NOT EXISTS variants text[] NOT NULL DEFAULT '{}';

-- Portefeuille diversifié en paper trading (services/engine/portfolio) -----
-- cours journaliers (Yahoo Finance) des ETF et du BTC utilisés par le portefeuille
CREATE TABLE IF NOT EXISTS pf_prices (
    symbol text NOT NULL,
    d      date NOT NULL,
    open   double precision NOT NULL,
    close  double precision NOT NULL,
    adj    double precision NOT NULL,
    PRIMARY KEY (symbol, d)
);
-- état du portefeuille virtuel (clé 'book') et suivi des exécutions (clé 'run')
CREATE TABLE IF NOT EXISTS pf_meta (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- valeur quotidienne par poche (rotation, rsi2, btc) et totale
CREATE TABLE IF NOT EXISTS pf_nav (
    d        date NOT NULL,
    sleeve   text NOT NULL,
    nav      double precision NOT NULL,
    exposure double precision,
    PRIMARY KEY (d, sleeve)
);
-- journal : ordres exécutés et signaux
CREATE TABLE IF NOT EXISTS pf_events (
    id         bigserial PRIMARY KEY,
    d          date NOT NULL,
    kind       text NOT NULL,
    payload    jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pf_events_d ON pf_events (d DESC);
