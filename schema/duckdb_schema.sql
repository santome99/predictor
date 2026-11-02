-- ============================================================================
-- Runtime schema (DuckDB >= 0.10) aplicado por ingestion/db.py
-- Tablas NORMALIZADAS que escribe la ingesta inicial (list_games + get_game
-- + get_ratings). El schema completo/analitico esta en strikerapi_schema.sql
-- (referencia y fases posteriores: events, shots, lineups, props, movement).
-- Convenio: PK compuesta (competition, id) porque /v1/laliga y /v1/ucl son
-- namespaces separados y los ids no son globales.
-- Idempotencia: INSERT OR REPLACE sobre las PK en el codigo Python.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Dimensiones
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS competitions (
    competition      VARCHAR PRIMARY KEY,      -- 'laliga' | 'ucl'
    name             VARCHAR,
    api_namespace    VARCHAR,                  -- 'laliga' | 'ucl'  (league=)
    results_start    INTEGER,
    box_score_start  INTEGER,
    shots_start      INTEGER,
    odds_start       INTEGER,
    props_start      VARCHAR
);

CREATE TABLE IF NOT EXISTS teams (
    competition  VARCHAR NOT NULL,
    team_id      VARCHAR NOT NULL,
    abbrev       VARCHAR,
    name         VARCHAR,
    display_name VARCHAR,
    short_name   VARCHAR,
    location     VARCHAR,
    color        VARCHAR,
    logo         VARCHAR,
    PRIMARY KEY (competition, team_id)
);

CREATE TABLE IF NOT EXISTS team_seasons (
    competition VARCHAR NOT NULL,
    season      INTEGER NOT NULL,
    team_id     VARCHAR NOT NULL,
    abbrev      VARCHAR,
    name        VARCHAR,
    PRIMARY KEY (competition, season, team_id)
);

-- ---------------------------------------------------------------------------
-- Partidos (list_games / get_game)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS games (
    competition   VARCHAR NOT NULL,
    game_id       VARCHAR NOT NULL,
    season        INTEGER,
    stage         VARCHAR,
    date          DATE,
    starts_at     TIMESTAMP,                   -- ISO 8601 UTC
    status        VARCHAR,                     -- 'scheduled' | 'final'
    status_detail VARCHAR,
    home_team_id  VARCHAR,
    away_team_id  VARCHAR,
    home_score    INTEGER,
    away_score    INTEGER,
    result        VARCHAR,                     -- 'H' | 'A' | 'D'
    neutral_site  BOOLEAN,
    venue         VARCHAR,
    city          VARCHAR,
    attendance    INTEGER,
    PRIMARY KEY (competition, game_id)
);
CREATE INDEX IF NOT EXISTS idx_games_comp_season ON games (competition, season);
CREATE INDEX IF NOT EXISTS idx_games_comp_date   ON games (competition, date);
CREATE INDEX IF NOT EXISTS idx_games_home        ON games (home_team_id);

-- ---------------------------------------------------------------------------
-- Box score por equipo (get_game.team_stats)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS team_match_stats (
    competition         VARCHAR NOT NULL,
    game_id             VARCHAR NOT NULL,
    team_id             VARCHAR NOT NULL,
    home                BOOLEAN,
    goals               INTEGER,
    xg                  DOUBLE,                -- xG API: NO es as-of (ver docs)
    possession_pct      DOUBLE,
    shots               INTEGER,
    shots_on_target     INTEGER,
    blocked_shots       INTEGER,
    corners             INTEGER,
    fouls               INTEGER,
    yellow_cards        INTEGER,
    red_cards           INTEGER,
    offsides            INTEGER,
    saves               INTEGER,
    passes              INTEGER,
    accurate_passes     INTEGER,
    crosses             INTEGER,
    accurate_crosses    INTEGER,
    long_balls          INTEGER,
    accurate_long_balls INTEGER,
    tackles             INTEGER,
    effective_tackles   INTEGER,
    interceptions       INTEGER,
    clearances          INTEGER,
    penalty_goals       INTEGER,
    penalty_attempts    INTEGER,
    PRIMARY KEY (competition, game_id, team_id)
);

-- ---------------------------------------------------------------------------
-- Lineas por jugador (get_game.players)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS player_match_stats (
    competition      VARCHAR NOT NULL,
    game_id          VARCHAR NOT NULL,
    player_id        VARCHAR NOT NULL,
    player_name      VARCHAR,
    team_id          VARCHAR,
    starter          BOOLEAN,
    position         VARCHAR,
    jersey           VARCHAR,
    minute_on        INTEGER,
    minute_off       INTEGER,
    goals            INTEGER,
    assists          INTEGER,
    shots            INTEGER,
    shots_on_target  INTEGER,
    fouls_committed  INTEGER,
    fouls_suffered   INTEGER,
    offsides         INTEGER,
    yellow_cards     INTEGER,
    red_cards        INTEGER,
    own_goals        INTEGER,
    saves            INTEGER,
    goals_conceded   INTEGER,
    shots_faced      INTEGER,
    PRIMARY KEY (competition, game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_pms_player ON player_match_stats (player_id);

-- ---------------------------------------------------------------------------
-- Cuotas por casa (get_game.odds) -- una fila por casa y fuente
-- Se guarda el valor ORIGINAL (americano o nulo) y el decimal derivado.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS odds_books (
    competition           VARCHAR NOT NULL,
    game_id               VARCHAR NOT NULL,
    provider              VARCHAR NOT NULL,    -- '888sport', 'Bet 365', ...
    source                VARCHAR NOT NULL,    -- 'archive' | 'final'
    home_ml_open_orig     INTEGER,
    home_ml_open_dec      DOUBLE,
    home_ml_close_orig    INTEGER,
    home_ml_close_dec     DOUBLE,
    draw_ml_open_orig     INTEGER,
    draw_ml_open_dec      DOUBLE,
    draw_ml_close_orig    INTEGER,
    draw_ml_close_dec     DOUBLE,
    away_ml_open_orig     INTEGER,
    away_ml_open_dec      DOUBLE,
    away_ml_close_orig    INTEGER,
    away_ml_close_dec     DOUBLE,
    home_handicap_open    DOUBLE,
    home_handicap_close   DOUBLE,
    handicap_home_odds_orig INTEGER,
    handicap_home_odds_dec  DOUBLE,
    handicap_away_odds_orig INTEGER,
    handicap_away_odds_dec  DOUBLE,
    total_open            DOUBLE,
    total_close           DOUBLE,
    over_odds_orig        INTEGER,
    over_odds_dec         DOUBLE,
    under_odds_orig       INTEGER,
    under_odds_dec        DOUBLE,
    close_prob_raw        VARCHAR,              -- JSON tal cual (parse posterior)
    captured_at           TIMESTAMP,            -- pre-partido siempre
    PRIMARY KEY (competition, game_id, provider, source)
);
CREATE INDEX IF NOT EXISTS idx_odds_provider ON odds_books (provider);

-- ---------------------------------------------------------------------------
-- Elo por competicion (get_ratings, date = as_of)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS elo_snapshots (
    competition    VARCHAR NOT NULL,
    as_of          DATE NOT NULL,
    rank           INTEGER,
    team_id        VARCHAR NOT NULL,
    abbrev         VARCHAR,
    name           VARCHAR,
    elo            DOUBLE,
    last_game      DATE,
    PRIMARY KEY (competition, as_of, team_id)
);
CREATE INDEX IF NOT EXISTS idx_elo_team ON elo_snapshots (team_id);

-- ============================================================================
-- OPERACIONAL / TELEMETRIA
-- ============================================================================

-- Jobs de ingesta (una fila por ejecucion)
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id        VARCHAR PRIMARY KEY,          -- uuid
    started_at    TIMESTAMP DEFAULT now(),
    finished_at   TIMESTAMP,
    competition   VARCHAR,
    season        INTEGER,
    mode          VARCHAR,                      -- 'run' | 'resume' | 'retry' | 'elp'
    status        VARCHAR DEFAULT 'running',    -- running | completed | aborted | error
    games_found   INTEGER,
    games_done    INTEGER,
    games_skipped INTEGER,
    games_failed  INTEGER,
    credits_used  INTEGER DEFAULT 0,
    message       VARCHAR
);

-- Checkpoint por partido: reanudacion sin re-comprar credito
CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
    competition      VARCHAR NOT NULL,
    season           INTEGER,
    game_id          VARCHAR NOT NULL,
    starts_at        TIMESTAMP,
    ingestion_status VARCHAR NOT NULL,          -- PENDING | PROCESSING | COMPLETED | FAILED | RETRY_PENDING
    attempts         INTEGER DEFAULT 0,
    last_attempt     TIMESTAMP,
    completed_at     TIMESTAMP,
    error_message    VARCHAR,
    PRIMARY KEY (competition, game_id)
);
CREATE INDEX IF NOT EXISTS idx_cp_status ON ingestion_checkpoints (ingestion_status);

-- Consumo real de creditos por mes, credencial y tool (contabilidad local).
-- El gasto se cuenta POR CREDENCIAL (clave API): asi se pueden hacer tandas
-- con claves distintas sin que una bloquee a la siguiente.
CREATE TABLE IF NOT EXISTS api_usage (
    month      VARCHAR NOT NULL,                -- 'YYYY-MM'
    credential VARCHAR NOT NULL,                -- etiqueta o hash de la API key
    tool       VARCHAR NOT NULL,
    credits    INTEGER NOT NULL,
    calls      INTEGER NOT NULL,
    PRIMARY KEY (month, credential, tool)
);

-- Indice de respuestas crudas (los JSON se guardan EN DISCO en raw/)
CREATE TABLE IF NOT EXISTS raw_api_responses (
    competition VARCHAR NOT NULL,
    tool        VARCHAR NOT NULL,               -- 'list_games' | 'get_game' | 'get_ratings'
    raw_key     VARCHAR NOT NULL,               -- identificador del contenido, p.ej game:740916
    path        VARCHAR NOT NULL,
    fetched_at  TIMESTAMP DEFAULT now(),
    credits     INTEGER,
    size_bytes  INTEGER,
    PRIMARY KEY (competition, tool, raw_key)
);