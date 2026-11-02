"""Builder: DuckDB -> ONE-ROW-PER-MATCH feature matrix (X + targets).

DECISION_D10 — One row per match. Everything the models consume (team form,
Elo, odds consensus, targets) is flat in a single row. Single-row-per-entity
matrices are the lingua franca of tabular ML (XGBoost/LightGBM-style), drop
into pandas/sklearn directly, and are easy to freeze as Parquet for
reproducibility.

DECISION_D11 — Two separate matrices: ``train_matrix`` (only rows whose
``starts_at`` is strictly BEFORE the freeze timestamp AND are finals) and the
live fixture matrix built on the fly for predictions. Prediction rows are
excluded from any matrix used for model fitting — the freeze snapshot encodes
this with a ``frozen`` column so re-running the builder is idempotent.

DECISION_D12 — Freeze by ``starts_at < now`` at build time; no future rows
(e.g., scheduled fixtures) leak into training. Idempotent re-build:
the matrix can be regenerated any time from DuckDB since all input rows come
from the immutable raw JSON layer.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np
import pandas as pd

from features.config import FeatureConfig
from features.context import elo_asof
from features.odds import game_1x2_features, game_totals_features
from features.windows import (attach_rolling_features, build_team_log,
                              match_features_from_log)


TARGET_COLS = ["home_win", "draw_game", "away_win", "btts", "total_goals",
               "over_25"]


class MatrixBuilder:
    def __init__(self, cfg: FeatureConfig):
        self.cfg = cfg
        self.conn = duckdb.connect(str(cfg.db_path), read_only=True)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ data
    def load_raw(self) -> dict:
        """Pull the four working tables as plain DataFrames."""
        c = self.cfg.competition
        out = {}
        out["games"] = self.conn.execute(
            "SELECT competition, game_id, season, stage, date, starts_at, status,"
            " result, home_team_id, away_team_id, home_score, away_score, venue,"
            " city FROM games WHERE competition=? AND status='final'"
            " AND home_score IS NOT NULL AND away_score IS NOT NULL"
            " ORDER BY starts_at, game_id", [c]).df()
        out["tms"] = self.conn.execute(
            "SELECT competition, game_id, team_id, home, goals, xg,"
            " possession_pct, shots, shots_on_target, blocked_shots, corners,"
            " fouls, yellow_cards, red_cards, offsides, saves, passes,"
            " accurate_passes, crosses, accurate_crosses, long_balls,"
            " accurate_long_balls, tackles, effective_tackles, interceptions,"
            " clearances, penalty_goals, penalty_attempts"
            " FROM team_match_stats WHERE competition=?", [c]).df()
        out["odds"] = self.conn.execute(
            "SELECT competition, game_id, provider, source, home_ml_open_orig,"
            " home_ml_open_dec, home_ml_close_orig, home_ml_close_dec,"
            " draw_ml_open_orig, draw_ml_open_dec, draw_ml_close_orig,"
            " draw_ml_close_dec, away_ml_open_orig, away_ml_open_dec,"
            " away_ml_close_orig, away_ml_close_dec, home_handicap_open,"
            " home_handicap_close, handicap_home_odds_orig, handicap_home_odds_dec,"
            " handicap_away_odds_orig, handicap_away_odds_dec, total_open,"
            " total_close, over_odds_orig, over_odds_dec, under_odds_orig,"
            " under_odds_dec, close_prob_raw, captured_at"
            " FROM odds_books WHERE competition=?", [c]).df()
        out["elo"] = self.conn.execute(
            "SELECT competition, team_id, as_of, elo, rank, last_game"
            " FROM elo_snapshots WHERE competition=? ORDER BY team_id, as_of",
            [c]).df()
        return out

    # ------------------------------------------------------------- assemble
    def build(self, freeze_at: Optional[str] = None) -> pd.DataFrame:
        """Build the frozen training matrix (targets included)."""
        raw = self.load_raw()
        # DECISION_D12b — all timestamps stay tz-naive UTC from DuckDB; the
        # freeze boundary must be tz-naive, otherwise comparisons raise.
        if freeze_at:
            freeze = pd.Timestamp(freeze_at)
        else:
            freeze = pd.Timestamp(pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%S"))
        if freeze.tzinfo is not None:
            freeze = freeze.tz_convert("UTC").tz_localize(None)
        games = raw["games"].copy()
        games = games[pd.to_datetime(games["starts_at"], utc=True).dt.tz_localize(None) < freeze]

        log = build_team_log(games, raw["tms"])
        log = attach_rolling_features(log, self.cfg.windows, self.cfg.min_periods)
        m = match_features_from_log(log)

        m = elo_asof(m, raw["elo"])

        # odds consensus per game (1X2 + totals)
        odds_feats = []
        for gid, grp in raw["odds"].groupby("game_id"):
            row = game_1x2_features(grp)
            row["game_id"] = gid
            row = row.merge(game_totals_features(grp), left_index=True,
                            right_index=True)
            odds_feats.append(row)
        if odds_feats:
            odds_df = pd.concat(odds_feats, ignore_index=True)
        else:
            odds_df = pd.DataFrame(columns=["game_id", "n_books"])
        m = m.merge(odds_df, on="game_id", how="left")

        m = self._add_targets(m)
        m["frozen_at"] = freeze
        # order columns: keys, features, targets
        keycols = ["competition", "game_id", "season", "date", "starts_at"]
        featcols = [c for c in m.columns
                    if c not in keycols + TARGET_COLS + ["frozen_at", "status"]]
        target = [c for c in TARGET_COLS if c in m.columns]
        other = [c for c in m.columns
                 if c not in keycols + featcols + target + ["frozen_at"]]
        return m[keycols + featcols + other + target + ["frozen_at"]]

    @staticmethod
    def _add_targets(m: pd.DataFrame) -> pd.DataFrame:
        m["home_win"] = (m["home_score"] > m["away_score"]).astype(int)
        m["draw_game"] = (m["home_score"] == m["away_score"]).astype(int)
        m["away_win"] = (m["home_score"] < m["away_score"]).astype(int)
        m["total_goals"] = m["home_score"] + m["away_score"]
        m["btts"] = ((m["home_score"] >= 1) & (m["away_score"] >= 1)).astype(int)
        m["over_25"] = (m["total_goals"] > 2.5).astype(int)
        return m

    # ------------------------------------------------------------------ save
    def save_matrix_and_meta(self, matrix: pd.DataFrame) -> dict:
        self.cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
        matrix.to_parquet(self.cfg.matrix_path, index=False)
        meta = {
            "built_at": pd.Timestamp.utcnow().isoformat(),
            "competition": self.cfg.competition,
            "rows": int(len(matrix)),
            "columns": int(len(matrix.columns)),
            "feature_columns": [c for c in matrix.columns
                                if c not in TARGET_COLS + ["frozen_at"]],
        }
        self.cfg.features_meta_path.write_text(json.dumps(meta, indent=2))
        return meta


def load_saved_matrix(cfg: FeatureConfig) -> pd.DataFrame:
    return pd.read_parquet(cfg.matrix_path)


def add_anytime_target(matrix):  # noqa: D401
    """placeholder: target expansion kept minimal for v1"""
    return matrix