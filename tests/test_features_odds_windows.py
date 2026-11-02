"""Tests: odds maths (margin removal), rolling anti-leakage, backtest integrity."""
import numpy as np
import pandas as pd
import pytest

from features.odds import (american_col_to_decimal, game_1x2_features,
                           implied_from_decimal, overround, remove_margin)
from features.windows import attach_rolling_features, build_team_log


def test_american_conversion_vector():
    s = pd.Series([105.0, -118.0, np.nan, 250.0])
    out = american_col_to_decimal(s)
    assert out.iloc[0] == pytest.approx(2.05)
    assert out.iloc[1] == pytest.approx(1.8475, rel=1e-3)
    assert np.isnan(out.iloc[2])
    assert out.iloc[3] == pytest.approx(3.5)


def test_overround_of_fair_book_is_zero():
    assert overround(np.array([2.0, 3.0, 6.0])) == pytest.approx(0.0, abs=1e-9)


def test_remove_margin_normalises():
    raw = np.array([1 / 2.1, 1 / 3.2, 1 / 4.0])
    fair = remove_margin(raw)
    assert fair.sum() == pytest.approx(1.0)
    # relative ordering preserved
    assert fair[0] > fair[1] > fair[2]


def test_remove_margin_with_nan_returns_all_nan():
    fair = remove_margin(np.array([1 / 2.0, np.nan, 1 / 3.0]))
    assert np.isnan(fair).all()


def test_1x2_features_ignore_incomplete_books():
    books = pd.DataFrame([{
        "home_ml_close_dec": 2.1, "draw_ml_close_dec": 3.2, "away_ml_close_dec": np.nan,
        "home_ml_open_dec": 2.0, "draw_ml_open_dec": 3.0, "away_ml_open_dec": 4.0,
    }])
    out = game_1x2_features(books)
    assert out.iloc[0]["n_books"] == 0
    assert np.isnan(out.iloc[0]["p_home_close"])


def test_rolling_only_uses_past_matches():
    """THE core anti-leakage invariant: rolling features at row k never include
    row k's own stats, only strictly previous rows."""
    games = pd.DataFrame([
        {"competition": "laliga", "game_id": "1", "season": 2025,
         "date": "2025-09-01", "starts_at": "2025-09-01T19:00:00Z",
         "status": "final", "result": "H", "home_team_id": "T1",
         "away_team_id": "T2", "home_score": 1, "away_score": 0,
         "venue": "V", "city": "C"},
        {"competition": "laliga", "game_id": "2", "season": 2025,
         "date": "2025-09-08", "starts_at": "2025-09-08T19:00:00Z",
         "status": "final", "result": "H", "home_team_id": "T1",
         "away_team_id": "T3", "home_score": 3, "away_score": 1,
         "venue": "V", "city": "C"},
        {"competition": "laliga", "game_id": "3", "season": 2025,
         "date": "2025-09-15", "starts_at": "2025-09-15T19:00:00Z",
         "status": "final", "result": "H", "home_team_id": "T1",
         "away_team_id": "T4", "home_score": 5, "away_score": 0,
         "venue": "V", "city": "C"},
    ])
    tms = pd.DataFrame(columns=["competition", "game_id", "team_id"])
    log = build_team_log(games, tms)
    feats = attach_rolling_features(log, windows=(5,), min_periods=1)
    t1 = feats[feats["team_id"] == "T1"].sort_values("starts_at")
    assert np.isnan(t1.iloc[0]["lag_goals_for"])   # no hay pasado
    assert t1.iloc[1]["lag_goals_for"] == 1.0      # media del partido 1
    assert t1.iloc[2]["lag_goals_for"] == 2.0      # media de partidos 1 y 2, NUNCA incluye el 3
    assert t1.iloc[2]["season_pts_before"] == 6    # 6 puntos antes del partido 3


def test_rest_days_and_points_are_past_only():
    games = pd.DataFrame([
        {"competition": "laliga", "game_id": "1", "season": 2025,
         "date": "2025-09-01", "starts_at": "2025-09-01T19:00:00Z",
         "status": "final", "result": "H", "home_team_id": "T1",
         "away_team_id": "T2", "home_score": 1, "away_score": 0,
         "venue": "V", "city": "C"},
        {"competition": "laliga", "game_id": "2", "season": 2025,
         "date": "2025-09-08", "starts_at": "2025-09-08T19:00:00Z",
         "status": "final", "result": "D", "home_team_id": "T1",
         "away_team_id": "T3", "home_score": 2, "away_score": 2,
         "venue": "V", "city": "C"},
    ])
    tms = pd.DataFrame(columns=["competition", "game_id", "team_id"])
    log = build_team_log(games, tms)
    feats = attach_rolling_features(log, windows=(5,), min_periods=1)
    t1 = feats[feats["team_id"] == "T1"].sort_values("starts_at")
    assert np.isnan(t1.iloc[0]["rest_days"])
    assert t1.iloc[1]["rest_days"] == pytest.approx(7.0)
    assert np.isnan(t1.iloc[0]["season_pts_before"])
    assert t1.iloc[1]["season_pts_before"] == 3  # el empate(1pt) no cuenta


def test_elo_asof_strictly_before(tmp_path):
    from features.context import elo_asof
    matches = pd.DataFrame([{
        "game_id": "gX", "competition": "laliga", "date": "2025-09-15",
        "starts_at": "2025-09-15T19:00:00Z", "home_team_id": "T1",
        "away_team_id": "T2",
    }])
    elo = pd.DataFrame([
        {"competition": "laliga", "team_id": "T1", "as_of": "2025-09-01",
         "elo": 1600.0, "rank": 1, "last_game": "2025-08-31"},
        # same-day snapshot: must NOT be attached (would include the match)
        {"competition": "laliga", "team_id": "T1", "as_of": "2025-09-15",
         "elo": 1800.0, "rank": 1, "last_game": "2025-09-15"},
    ])
    out = elo_asof(matches, elo)
    assert out.iloc[0]["h_elo"] == 1600.0   # only the pre-match snapshot