"""Predictions for upcoming fixtures.

DECISION_D20 — Reuse the SAME pipeline for prediction as for training. The
only difference: fixtures have no result yet, so the matrix builder must
support rows without targets. Feature names must be identical to the frozen
training matrix (feature_columns.json is the contract between the two).

DECISION_D21 — Prediction features for a fixture at time T are built as-of
NOW including matches finished by now. This is not leakage: by definition
"now" precedes kick-off, everything used is real pre-match information.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Optional

import numpy as np
import pandas as pd

from features.config import FeatureConfig
from features.context import elo_asof
from features.models import Ensemble, GBMResultModel, PoissonGoalsModel
from features.windows import (attach_rolling_features, build_team_log,
                              match_features_from_log)


def load_fixtures(cfg: FeatureConfig, conn) -> pd.DataFrame:
    """Scheduled fixtures in DB: status != 'final' with future starts_at."""
    return conn.execute(
        "SELECT competition, game_id, season, stage, date, starts_at, status,"
        " home_team_id, away_team_id FROM games WHERE status != 'final'"
        " AND starts_at >= CURRENT_TIMESTAMP ORDER BY starts_at",
        ).df()


def load_past_log(cfg: FeatureConfig, conn) -> pd.DataFrame:
    """All finished matches (their results are INPUT for form features)."""
    return conn.execute(
        "SELECT competition, game_id, season, stage, date, starts_at, status,"
        " result, home_team_id, away_team_id, home_score, away_score, venue,"
        " city FROM games WHERE status='final' AND home_score IS NOT NULL"
        " ORDER BY starts_at, game_id").df()


def build_fixture_features(cfg: FeatureConfig, fixtures: pd.DataFrame,
                           past: pd.DataFrame, tms: pd.DataFrame,
                           elo: pd.DataFrame) -> pd.DataFrame:
    """Attach as-of-NOW features to fixtures.

    Fixture rows themselves have no stats/odds; we build the team log from
    PAST matches only and carry the latest lag values forward onto the
    fixture row for each team.
    """
    log = build_team_log(past, tms)
    log = attach_rolling_features(log, cfg.windows, cfg.min_periods)
    # the freshest rolling snapshot exists on the LAST past row per team:
    # keep only the last row per (competition, team)
    latest = (log.sort_values(["competition", "team_id", "starts_at"])
              .groupby(["competition", "team_id"], sort=False).tail(1))
    lag_cols = [c for c in latest.columns
                if c.startswith("lag_") or c in
                ("rest_days", "season_pts_before", "season_gd_before")]

    fx = fixtures.copy()
    for side, team_col, prefix in (("h", "home_team_id", "h_"),
                                   ("a", "away_team_id", "a_")):
        sub = latest[["competition", "team_id"] + lag_cols]
        sub = sub.rename(columns={c: f"{prefix}{c}" for c in lag_cols})
        fx = fx.merge(sub, left_on=["competition", team_col],
                      right_on=["competition", "team_id"], how="left")
        fx.drop(columns=["team_id"], errors="ignore", inplace=True)
    fx = elo_asof(fx, elo)
    return fx


def predict_fixtures(cfg: FeatureConfig, fixtures: pd.DataFrame,
                     training_matrix: pd.DataFrame,
                     poisson: PoissonGoalsModel,
                     gbm: Optional[GBMResultModel]) -> pd.DataFrame:
    """Add model probabilities (1X2 + score markets) to the fixtures frame."""
    ens = Ensemble(poisson, gbm, weight=cfg.poisson_weight)
    probs = ens.predict_proba_1x2(fixtures)
    score = ens.predict_score_market(fixtures, lines=cfg.cross_over_lines)

    out = fixtures.copy()
    out["p_home"], out["p_draw"], out["p_away"] = probs.T
    out["pick"] = np.choose(np.argmax(probs, axis=1),
                            ["1", "X", "2"])
    out = out.merge(score, on="game_id", how="left")
    out = out.sort_values("starts_at")
    return out


def save_predictions(out: pd.DataFrame, cfg: FeatureConfig) -> None:
    cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.artifacts_dir / "predictions.parquet"
    out.to_parquet(path, index=False)
    print(f"Predicciones guardadas en {path}")