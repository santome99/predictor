-- ============================================================================
-- StrikerAPI Dataset Schema (DuckDB >= 0.10)
-- Proyecto: prediccion de apuestas (La Liga + UEFA Champions League)
-- Disena: capa curada / core, modelada a partir de los 19 endpoints
-- Convencion: PK compuesta (competition, id) porque cada competicion tiene su
--   propio namespace de la API (/v1/laliga, /v1/ucl) y los ids no son globales.
-- Guarda SIEMPRE el JSON crudo de cada llamada en la capa raw/ (fuente de
--   verdad inmutable); estas tablas son la capa parseada/normalizada.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Dimensiones
-- ---------------------------------------------------------------------------

CREATE TABLE competitions (
    competition     VARCHAR PRIMARY KEY,      -- 'laliga' | 'ucl'
    name            VARCHAR NOT NULL,
    api_namespace   VARCHAR NOT NULL,         -- 'laliga' | 'ucl' (league=)
    results_start   INTEGER,                  -- 2001
    box_score_start INTEGER,                  -- laliga 2005, ucl 2012 (grupos/KO)
    shots_start     INTEGER,                  -- 2012
    odds_start      INTEGER,                  -- 2017
    props_start     VARCHAR                   -- '2024-08' laliga / '2025-03' ucl
);

CREATE TABLE teams (
    team_id     VARCHAR NOT NULL,
    competition VARCHAR NOT NULL,
    abbrev      VARCHAR,
    name        VARCHAR,
    display_name VARCHAR,
    short_name  VARCHAR,
    location    VARCHAR,
    color       VARCHAR,
    logo        VARCHAR,
    PRIMARY KEY (competition, team_id)
);

-- Un club puede aparecer en varias temporadas; contendio sin repetir teams.
CREATE TABLE team_seasons (
    competition VARCHAR NOT NULL,
    season      INTEGER NOT NULL,
    team_id     VARCHAR NOT NULL,
    abbrev      VARCHAR,
    name        VARCHAR,
    PRIMARY KEY (competition, season, team_id)
);

CREATE TABLE players (
    competition VARCHAR NOT NULL,
    player_id   VARCHAR NOT NULL,
    name        VARCHAR,
    short_name  VARCHAR,
    position    VARCHAR,
    jersey      VARCHAR,
    team_id     VARCHAR,
    last_season INTEGER,
    appearances INTEGER,
    PRIMARY KEY (competition, player_id)
);

-- ---------------------------------------------------------------------------
-- Partidos (list_games) + targets derivados
-- ---------------------------------------------------------------------------

CREATE TABLE games (
    competition  VARCHAR NOT NULL,
    game_id      VARCHAR NOT NULL,
    season       INTEGER NOT NULL,
    stage        VARCHAR,                      -- 'Regular Season', 'League Phase', 'Round of 16'...
    stage_type   VARCHAR,                      -- 'league' | 'knockout' (param stage de list_games)
    date         DATE,
    starts_at    TIMESTAMP,                    -- ISO 8601 UTC
    status       VARCHAR,                      -- 'scheduled' | 'final'
    status_detail VARCHAR,                     -- 'FT', etc.
    home_team_id VARCHAR,
    away_team_id VARCHAR,
    home_score   INTEGER,
    away_score   INTEGER,
    result       VARCHAR,                      -- 'H' | 'A' | 'D' (null si scheduled)
    neutral_site BOOLEAN,                      -- importante en UCL (finales)
    venue        VARCHAR,
    city         VARCHAR,
    attendance   INTEGER,
    PRIMARY KEY (competition, game_id),
    FOREIGN KEY (competition, home_team_id) REFERENCES teams (competition, team_id),
    FOREIGN KEY (competition, away_team_id) REFERENCES teams (competition, team_id)
);
CREATE INDEX idx_games_comp_season ON games (competition, season);
CREATE INDEX idx_games_comp_date   ON games (competition, date);
CREATE INDEX idx_games_home        ON games (competition, home_team_id);
CREATE INDEX idx_games_away        ON games (competition, away_team_id);

-- ---------------------------------------------------------------------------
-- Box scores por equipo (get_game.team_stats)
-- ---------------------------------------------------------------------------

CREATE TABLE team_match_stats (
    competition         VARCHAR NOT NULL,
    game_id             VARCHAR NOT NULL,
    team_id             VARCHAR NOT NULL,
    home                BOOLEAN NOT NULL,
    goals               INTEGER,
    xg                  DOUBLE,
    possession_pct      DOUBLE,                -- null en algunas ligas/temporadas
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
    PRIMARY KEY (competition, game_id, team_id),
    FOREIGN KEY (competition, game_id) REFERENCES games (competition, game_id),
    FOREIGN KEY (competition, team_id) REFERENCES teams (competition, team_id)
);
CREATE INDEX idx_tms_team ON team_match_stats (competition, team_id);

-- ---------------------------------------------------------------------------
-- Lineas por jugador (get_game.players)
-- ---------------------------------------------------------------------------

CREATE TABLE player_match_stats (
    competition    VARCHAR NOT NULL,
    game_id        VARCHAR NOT NULL,
    player_id      VARCHAR NOT NULL,
    team_id        VARCHAR,
    starter        BOOLEAN,
    position       VARCHAR,
    jersey         VARCHAR,
    minute_on      INTEGER,
    minute_off     INTEGER,
    goals          INTEGER,
    assists        INTEGER,
    shots          INTEGER,
    shots_on_target INTEGER,
    fouls_committed INTEGER,
    fouls_suffered INTEGER,
    offsides       INTEGER,
    yellow_cards   INTEGER,
    red_cards      INTEGER,
    own_goals      INTEGER,
    saves          INTEGER,
    goals_conceded INTEGER,
    shots_faced    INTEGER,
    PRIMARY KEY (competition, game_id, player_id),
    FOREIGN KEY (competition, game_id) REFERENCES games (competition, game_id)
);
CREATE INDEX idx_pms_player ON player_match_stats (competition, player_id);

-- ---------------------------------------------------------------------------
-- Alineaciones (get_lineups)
-- ---------------------------------------------------------------------------

CREATE TABLE lineups (
    competition VARCHAR NOT NULL,
    game_id     VARCHAR NOT NULL,
    team_id     VARCHAR NOT NULL,
    formation   VARCHAR,                       -- '4-2-3-1'
    PRIMARY KEY (competition, game_id, team_id)
);

CREATE TABLE lineup_player_entries (
    competition        VARCHAR NOT NULL,
    game_id            VARCHAR NOT NULL,
    team_id            VARCHAR NOT NULL,
    player_id          VARCHAR NOT NULL,
    in_starting_xi     BOOLEAN NOT NULL DEFAULT false,
    in_subs_used       BOOLEAN NOT NULL DEFAULT false,
    unused_sub         BOOLEAN NOT NULL DEFAULT false,
    jersey             VARCHAR,
    position           VARCHAR,
    formation_place    INTEGER,
    minute_on          INTEGER,
    minute_off         INTEGER,
    goals              INTEGER,
    assists            INTEGER,
    yellow_cards       INTEGER,
    red_cards          INTEGER,
    PRIMARY KEY (competition, game_id, team_id, player_id)
);

-- ---------------------------------------------------------------------------
-- Eventos del partido (get_events) - fila por evento
-- ---------------------------------------------------------------------------

CREATE TABLE match_events (
    competition VARCHAR NOT NULL,
    game_id     VARCHAR NOT NULL,
    source      VARCHAR NOT NULL DEFAULT 'key_events',  -- 'key_events' (get_game) | 'events' (get_events)
    seq         INTEGER NOT NULL,              -- orden en la API (key_events: ordinal sintetico)
    minute      INTEGER,
    added_minute INTEGER,
    period      INTEGER,                       -- 1 = primer tiempo (descanso = goles period 1)
    type        VARCHAR,                       -- goal, card, substitution, shot, foul, corner...
    team_id     VARCHAR,
    player_id   VARCHAR,
    player      VARCHAR,
    player2_id  VARCHAR,                       -- asistente
    player2     VARCHAR,
    is_goal     BOOLEAN,
    own_goal    BOOLEAN,
    shootout    BOOLEAN,                       -- true = gol en tanda de penaltis
    penalty     BOOLEAN,
    text        VARCHAR,
    PRIMARY KEY (competition, game_id, source, seq),
    FOREIGN KEY (competition, game_id) REFERENCES games (competition, game_id)
);
CREATE INDEX idx_events_type ON match_events (competition, game_id, type);

-- Tiros con localizacion y xG (get_shot_map / get_events kind=shots)
CREATE TABLE match_shots (
    competition VARCHAR NOT NULL,
    game_id     VARCHAR NOT NULL,
    source      VARCHAR NOT NULL,              -- 'shot_map' | 'events'
    seq         INTEGER NOT NULL,              -- shot_map: ordinal sintetico
    minute      INTEGER,
    period      INTEGER,
    team_id     VARCHAR,
    player_id   VARCHAR,
    player      VARCHAR,
    outcome     VARCHAR,                       -- goal, missed, blocked, saved...
    body_part   VARCHAR,                       -- right_foot, left_foot, header
    penalty     BOOLEAN,
    xg          DOUBLE,                        -- xG propio de StrikerAPI (NO es as-of)
    zone        VARCHAR,                       -- six_yard_box, penalty_area, outside_box
    x           DOUBLE,                        -- dist. al gol como fraccion de la mitad del campo
    y           DOUBLE,                        -- 0.5 = centro
    end_x       DOUBLE,
    end_y       DOUBLE,
    distance_m  DOUBLE,                        -- 'metres' en eventos, distancia real
    PRIMARY KEY (competition, game_id, source, seq),
    FOREIGN KEY (competition, game_id) REFERENCES games (competition, game_id)
);
CREATE INDEX idx_shots_player ON match_shots (competition, player_id);
CREATE INDEX idx_shots_team   ON match_shots (competition, team_id);

-- ---------------------------------------------------------------------------
-- Cuotas por casa (get_odds / get_game.odds) - una fila por casa y fuente
-- ---------------------------------------------------------------------------

CREATE TYPE cuota AS STRUCT(
    open  INTEGER,                             -- americana; null si no hay apertura
    close INTEGER
);

CREATE TABLE odds_books (
    competition        VARCHAR NOT NULL,
    game_id            VARCHAR NOT NULL,
    provider           VARCHAR NOT NULL,       -- '888sport', 'Bet 365', 'DraftKings'...
    source             VARCHAR NOT NULL,       -- 'archive' | 'final'
    home_ml_open       INTEGER,
    home_ml_close      INTEGER,
    draw_ml_open       INTEGER,
    draw_ml_close      INTEGER,
    away_ml_open       INTEGER,
    away_ml_close      INTEGER,
    home_prob          DOUBLE,                 -- parse de close_prob (sin margen)
    draw_prob          DOUBLE,
    away_prob          DOUBLE,
    home_handicap_open DOUBLE,
    home_handicap_close DOUBLE,
    handicap_home_odds INTEGER,
    handicap_away_odds INTEGER,
    total_open         DOUBLE,
    total_close        DOUBLE,
    over_odds          INTEGER,
    under_odds         INTEGER,
    close_prob_raw     VARCHAR,                 -- JSON tal cual devuelto por la API
    captured_at        TIMESTAMP,              -- pre-partido SIEMPRE (nunca in-play)
    PRIMARY KEY (competition, game_id, provider, source),
    FOREIGN KEY (competition, game_id) REFERENCES games (competition, game_id)
);
CREATE INDEX idx_odds_provider ON odds_books (provider);

-- Resumen por partido derivado de get_odds (medianas + outcome con el resultado)
CREATE TABLE odds_games_summary (
    competition            VARCHAR NOT NULL,
    game_id                VARCHAR NOT NULL,
    date                   DATE,
    n_books                INTEGER,
    med_home_ml_open       DOUBLE,
    med_draw_ml_open       DOUBLE,
    med_away_ml_open       DOUBLE,
    med_home_ml_close      DOUBLE,
    med_draw_ml_close      DOUBLE,
    med_away_ml_close      DOUBLE,
    med_home_prob          DOUBLE,              -- sin margen
    med_draw_prob          DOUBLE,
    med_away_prob          DOUBLE,
    med_total_close        DOUBLE,
    outcome_result         VARCHAR,             -- 'H' | 'A' | 'D'
    outcome_median_total   DOUBLE,
    outcome_home_handicap  DOUBLE,
    outcome_over           BOOLEAN,
    outcome_home_covered   BOOLEAN,
    captured_at            TIMESTAMP,
    PRIMARY KEY (competition, game_id)
);

-- Serie temporal pre-partido (get_line_movement)
CREATE TABLE odds_movement (
    competition   VARCHAR NOT NULL,
    game_id       VARCHAR NOT NULL,
    provider      VARCHAR NOT NULL,
    captured_at   TIMESTAMP NOT NULL,
    home_ml       INTEGER,
    draw_ml       INTEGER,
    away_ml       INTEGER,
    home_handicap DOUBLE,
    total         DOUBLE,
    PRIMARY KEY (competition, game_id, provider, captured_at),
    FOREIGN KEY (competition, game_id) REFERENCES games (competition, game_id)
);

-- ---------------------------------------------------------------------------
-- Goleador anytime (get_player_props / get_prop_history)
-- ---------------------------------------------------------------------------

CREATE TABLE player_props (
    competition  VARCHAR NOT NULL,
    game_id      VARCHAR NOT NULL,
    player_id    VARCHAR NOT NULL,
    player_name  VARCHAR,
    market       VARCHAR NOT NULL,             -- 'player_goal_scorer_anytime'
    bookmaker    VARCHAR NOT NULL,
    line         DOUBLE,                       -- null para este mercado
    over_odds    INTEGER,                      -- 'Yes', americana
    under_odds   INTEGER,                      -- 'No' (donde publique)
    captured_at  TIMESTAMP,
    snapshot_type VARCHAR,                     -- 'closing' | 'opening'
    source       VARCHAR,                      -- 'archive'...
    PRIMARY KEY (competition, game_id, player_id, bookmaker, market, snapshot_type)
);

-- ---------------------------------------------------------------------------
-- Contexto de temporada (features legitimos, consultar con fecha < partido)
-- ---------------------------------------------------------------------------

CREATE TABLE team_season_aggregates (          -- get_team_stats
    competition     VARCHAR NOT NULL,
    season          INTEGER NOT NULL,
    team_id         VARCHAR NOT NULL,
    games           INTEGER,
    goals_for       DOUBLE,
    goals_against   DOUBLE,
    xg_for          DOUBLE,
    xg_against      DOUBLE,
    shots_for       DOUBLE,
    shots_against   DOUBLE,
    shots_on_target_for DOUBLE,
    shots_on_target_against DOUBLE,
    possession_pct  DOUBLE,
    pass_accuracy_pct DOUBLE,
    passes          DOUBLE,
    corners         DOUBLE,
    fouls           DOUBLE,
    yellow_cards    DOUBLE,
    red_cards_total DOUBLE,
    tackles         DOUBLE,
    interceptions   DOUBLE,
    clean_sheets    INTEGER,
    failed_to_score INTEGER,
    PRIMARY KEY (competition, season, team_id)
);

CREATE TABLE standings_snapshots (             -- get_standings
    competition  VARCHAR NOT NULL,
    season       INTEGER NOT NULL,
    captured_on  DATE NOT NULL,                -- fecha de captura (<= partido para features)
    group_name   VARCHAR,
    rank         INTEGER,
    team_id      VARCHAR,
    abbrev       VARCHAR,
    display_name VARCHAR,
    played       INTEGER,
    wins         INTEGER,
    draws        INTEGER,
    losses       INTEGER,
    goals_for    INTEGER,
    goals_against INTEGER,
    goal_diff    INTEGER,
    points       INTEGER,
    deductions   INTEGER,
    note         VARCHAR,
    home_record  VARCHAR,                      -- '15-2-2' W-D-L
    away_record  VARCHAR,
    form         VARCHAR,                      -- 'WWWWW'
    PRIMARY KEY (competition, season, captured_on, rank)
);

CREATE TABLE elo_snapshots (                   -- get_ratings
    competition VARCHAR NOT NULL,
    as_of       DATE NOT NULL,
    rank        INTEGER,
    team_id     VARCHAR NOT NULL,
    abbrev      VARCHAR,
    name        VARCHAR,
    elo         DOUBLE,
    last_game   DATE,
    PRIMARY KEY (competition, as_of, team_id)
);

-- Totales por zona del mapa de tiros (get_shot_map.zones)
CREATE TABLE shot_map_zone_totals (
    competition VARCHAR NOT NULL,
    season      INTEGER NOT NULL,
    team_id     VARCHAR NOT NULL,
    against     BOOLEAN NOT NULL,              -- true = tiros concedidos
    zone        VARCHAR NOT NULL,
    shots       INTEGER,
    goals       INTEGER,
    on_target   INTEGER,
    headers     INTEGER,
    xg          DOUBLE,
    conversion  DOUBLE,
    share       DOUBLE,
    PRIMARY KEY (competition, season, team_id, against, zone)
);

-- ---------------------------------------------------------------------------
-- Telemetria / operaciones
-- ---------------------------------------------------------------------------

CREATE TABLE ingestion_log (
    fetched_at    TIMESTAMP DEFAULT now(),
    competition   VARCHAR,
    tool          VARCHAR,                     -- 'get_game', 'get_odds'...
    params        JSON,
    credits_used  INTEGER,
    credits_left  INTEGER,
    raw_file      VARCHAR,                     -- ruta al JSON crudo inmutable
    rows          INTEGER
);

CREATE TABLE credits_usage (                   -- suma mensual de gasto
    month         VARCHAR,                     -- 'YYYY-MM'
    tool          VARCHAR,
    credits       INTEGER,
    calls         INTEGER,
    PRIMARY KEY (month, tool)
);

-- Estado de ingesta: permite reanudar sin recomprar credito por sietes
CREATE TABLE ingest_state (
    key       VARCHAR PRIMARY KEY,             -- p.ej. 'game:740968', 'shots:2023:ARN:false'
    status    VARCHAR,                         -- 'ok' | 'error' | 'empty'
    raw_file  VARCHAR,
    updated_at TIMESTAMP DEFAULT now()
);

-- ===========================================================================
-- Views de trabajo (features / targets) - anti-fuga en la consulta
-- ===========================================================================

-- 1) Targets derivados por partido final
CREATE OR REPLACE VIEW match_targets AS
SELECT
    g.competition, g.game_id, g.season, g.date, g.stage, g.status,
    g.home_team_id, g.away_team_id,
    g.home_score, g.away_score,
    g.result,
    g.home_score > g.away_score                 AS home_win,
    g.home_score = g.away_score                 AS draw_game,
    g.away_score > g.home_score                 AS away_win,
    g.home_score + g.away_score                 AS total_goals,
    (g.home_score >= 1 AND g.away_score >= 1)   AS btts,
    g.home_score - g.away_score                 AS home_margin
FROM games g
WHERE g.status = 'final';

-- 2) Historia del descanso (resultado HT) derivada de eventos period=1
CREATE OR REPLACE VIEW half_time_targets AS
SELECT
    me.competition, me.game_id,
    SUM(CASE WHEN me.period = 1 AND me.is_goal AND me.team_id = g.home_team_id THEN 1 ELSE 0 END) AS ht_home_goals,
    SUM(CASE WHEN me.period = 1 AND me.is_goal AND me.team_id = g.away_team_id THEN 1 ELSE 0 END) AS ht_away_goals
FROM match_events me
JOIN games g USING (competition, game_id)
WHERE me.is_goal AND NOT me.shootout      -- excluye penaltis de la tanda
GROUP BY 1, 2;

-- 3) Matriz de features: una fila por partido, columnas = cuotas de cierre por
--    casa, probs sin margen, Elo, medias de temporada, form. USAR SOLO con
--    capturas pre-partido (odds es pre por definicion; elo/standings capturadas
--    con as_of/captured_on < date del partido).
CREATE OR REPLACE VIEW feature_one_row_per_game AS
SELECT
    mt.competition, mt.game_id, mt.season, mt.date,
    mt.home_team_id, mt.away_team_id, mt.result,
    mt.home_win, mt.draw_game, mt.away_win,
    mt.total_goals, mt.btts, mt.home_margin,
    -- medianas de cierre 1X2 y total entre casas (solo pre-partido)
    median(o.home_ml_close)  FILTER (WHERE o.source = 'archive' OR TRUE)           AS med_home_ml_close,
    median(o.draw_ml_close)                                                         AS med_draw_ml_close,
    median(o.away_ml_close)                                                         AS med_away_ml_close,
    median(o.home_prob)                                                             AS med_home_prob,
    median(o.draw_prob)                                                             AS med_draw_prob,
    median(o.away_prob)                                                             AS med_away_prob,
    median(o.total_close)                                                           AS med_total_close,
    -- Elo pre-partido (estrictamente anterior a la fecha del partido)
    h.elo                                   AS home_elo,
    a.elo                                   AS away_elo,
    -- fuerza de temporada
    th.goals_for                            AS home_szn_goals_for,
    ta.goals_against                        AS away_szn_goals_against
FROM match_targets mt
JOIN odds_books o  ON o.competition = mt.competition AND o.game_id = mt.game_id
LEFT JOIN elo_snapshots h ON h.competition = mt.competition AND h.team_id = mt.home_team_id
    AND h.as_of = (SELECT max(h2.as_of) FROM elo_snapshots h2
                   WHERE h2.competition = mt.competition AND h2.team_id = h.team_id
                     AND h2.as_of < mt.date::DATE)
LEFT JOIN elo_snapshots a ON a.competition = mt.competition AND a.team_id = mt.away_team_id
    AND a.as_of = (SELECT max(a2.as_of) FROM elo_snapshots a2
                   WHERE a2.competition = mt.competition AND a2.team_id = a.team_id
                     AND a2.as_of < mt.date::DATE)
LEFT JOIN team_season_aggregates th ON th.competition = mt.competition AND th.season = mt.season AND th.team_id = mt.home_team_id
LEFT JOIN team_season_aggregates ta ON ta.competition = mt.competition AND ta.season = mt.season AND ta.team_id = mt.away_team_id
GROUP BY ALL;