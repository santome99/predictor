"""Rolling (as-of) features: team form computed ONLY from past matches.

DECISION_D6 — This is the core anti-leakage mechanism. Every feature attached
to a match at time ``t`` must be computable with information available at
``t - 1``. Concretely, when we build the team playing log (one row per
team-match, ordered by ``starts_at``), the rolling aggregate for window W is
computed with ``.rolling(W).mean().shift(1)``:

  - ``shift(1)`` pushes each value one row down the team's own timeline, so
    row k carries the mean of rows [k-W ... k-1]: strictly previous matches.
    The match's own row is NEVER included (that would be leakage).
  - ``min_periods`` (D9) tolerates a team's first games of a season where
    fewer than W previous matches exist -> produces NaN, no fake zeros.

This rule applies identically to Elo (as-of date strictly before the match)
because a rating captured after kick-off would silently "know" the result.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# stat columns that exist in team_match_stats and are safe to roll
ROLLING_STATS = [
    "goals", "xg", "shots", "shots_on_target", "blocked_shots", "corners",
    "fouls", "yellow_cards", "red_cards", "offsides", "saves", "passes",
    "accurate_passes", "crosses", "accurate_crosses", "long_balls",
    "accurate_long_balls", "tackles", "effective_tackles", "interceptions",
    "clearances", "possession_pct", "penalty_goals", "penalty_attempts",
]

# scores / results computed from the games table (per team, per match)
RESULT_STATS = ["goals_for", "goals_against", "points", "shots_for_margin"]


def build_team_log(games: pd.DataFrame, tms: pd.DataFrame) -> pd.DataFrame:
    """One row per (team, match) with that team's points of view.

    - ``side`` = home | away
    - two stacked frames (home team rows + away team rows) carrying the
    same match id so both perspectives share identity columns.
    """
    g = games.copy()

    def _mk(team_col, opp_col, side):
        f = g.copy()
        f["team_id"] = f[team_col]
        f["opp_team_id"] = f[opp_col]
        f["side"] = side
        f["goals_for"] = np.where(side == "home", f["home_score"], f["away_score"])
        f["goals_against"] = np.where(side == "home", f["away_score"],
                                      f["home_score"])
        return f

    home = _mk("home_team_id", "away_team_id", "home")
    away = _mk("away_team_id", "home_team_id", "away")

    log = pd.concat([home, away], ignore_index=True)
    log["goals_for"] = log["goals_for"].astype(float)
    log["goals_against"] = log["goals_against"].astype(float)
    log["points"] = np.select(
        [log["goals_for"] > log["goals_against"],
         log["goals_for"] == log["goals_against"]],
        [3, 1], default=0)

    # join per-team box score stats on (game_id, team_id)
    log = log.merge(
        tms,
        on=["competition", "game_id", "team_id"], how="left", suffixes=("", "_team"))

    log = log.sort_values(["competition", "team_id", "starts_at", "game_id"])
    log.reset_index(drop=True, inplace=True)
    return log


def attach_rolling_features(log: pd.DataFrame, windows: tuple,
                            min_periods: int) -> pd.DataFrame:
    """Add rolling past-only aggregates per team, plus rest days and season
    points accrued BEFORE the match."""
    base_cols = [c for c in ROLLING_STATS if c in log.columns] + RESULT_STATS

    grouped = log.groupby(["competition", "team_id"], sort=False)
    out = log.copy()
    for stat in base_cols:
        if stat not in out.columns:
            continue
        rolled = grouped[stat].rolling(
            window=max(windows), min_periods=min_periods).mean().shift(1)
        # reset multi-index to align with out
        out.loc[:, f"lag_{stat}"] = rolled.reset_index(level=[0, 1], drop=True).to_numpy()

    # rest days since previous match of the SAME team
    prev = grouped["starts_at"].shift(1)
    out.loc[:, "rest_days"] = (
        (pd.to_datetime(out["starts_at"]) - pd.to_datetime(prev))
        .dt.total_seconds() / 86400.0)
    out.loc[:, "rest_days"] = out["rest_days"].replace(np.inf, np.nan)

    # season-to-date points/goals BEFORE this match (cumsum shifted)
    out = out.sort_values(["competition", "team_id", "season", "starts_at"])
    g2 = out.groupby(["competition", "team_id", "season"], sort=False)
    out.loc[:, "season_pts_before"] = g2["points"].cumsum().shift(1)
    out.loc[:, "season_gd_before"] = (g2["goals_for"].cumsum() -
                                      g2["goals_against"].cumsum()).shift(1)

    return out


def match_features_from_log(log: pd.DataFrame) -> pd.DataFrame:
    """Split the team log into home and away feature frames and join them
    onto one row per match.

    The rolling lag_* were computed per team timeline. For each match we take
    the HOME team's lagged row and the AWAY team's lagged row and concatenate
    with ``h_``/``a_`` prefixes, so a single row contains both perspectives
    of the same pre-match state.
    """
    home_rows = log[log["side"] == "home"].copy()
    away_rows = log[log["side"] == "away"].copy()

    key_cols = ["competition", "game_id", "season", "date", "starts_at",
                "home_team_id", "away_team_id", "home_score", "away_score",
                "result", "status"]
    lag_cols = [c for c in log.columns
                if c.startswith("lag_") or c in ("rest_days", "season_pts_before",
                                                 "season_gd_before")]

    home = home_rows[key_cols + ["team_id"] + lag_cols].rename(
        columns={"team_id": "_home_team_id"})
    away = away_rows[key_cols + ["team_id"] + lag_cols].rename(
        columns={"team_id": "_away_team_id"})

    # align: home frame uses home_team_id; away frame uses away_team_id
    home = home.rename(columns={c: f"h_{c}" for c in lag_cols})
    away = away.rename(columns={c: f"a_{c}" for c in lag_cols})

    m = home.merge(away, on=key_cols, suffixes=("", "_a"))
    m.drop(columns=["_home_team_id", "_away_team_id"], errors="ignore", inplace=True)
    return m


def rolling_series_asof(df: pd.DataFrame) -> pd.DataFrame:
    """Compatibility helper: compute past-only rolling means given a sorted
    per-team frame. (Exposed for testing the shift-1 semantics.)"""
    return attach_rolling_features(df, windows=(5,), min_periods=1)