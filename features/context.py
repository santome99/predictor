"""Context joins with strict as-of semantics: Elo and competition position.

DECISION_D7 — Elo features are attached with a strict ``as_of_dt < match_dt``
comparison (the last rating strictly BEFORE the match). The API's ``as_of``
reflects ratings at end of that day; sharing the match date could include the
same day's result -> leakage. The snapshot's ``last_game`` column additionally
lets us discard a rating that already includes the match itself.

We use ``merge_asof(allow_exact_matches=False)`` on datetime: it performs a
binary search for the latest prior key instead of an O(n^2) correlated join.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def elo_asof(matches: pd.DataFrame, elo: pd.DataFrame) -> pd.DataFrame:
    """Attach latest pre-match Elo for home and away teams.

    ``elo``: rows (competition, team_id, as_of DATE, elo, last_game DATE).
    Returns matches with ``h_elo``, ``a_elo``, ``h_elo_last_game``,
    ``a_elo_last_game``.
    """
    if elo is None or len(elo) == 0:
        for c in ("h_elo", "a_elo"):
            matches[c] = np.nan
        return matches

    elo = elo.copy()
    elo["as_of_dt"] = pd.to_datetime(elo["as_of"])
    elo = elo.sort_values(["competition", "team_id", "as_of_dt"])

    to_join = elo[["competition", "team_id", "as_of_dt", "elo", "last_game"]].copy()
    # merge_asof exige la derecha ordenada por (by..., on) — la reordenamos.
    to_join = to_join.sort_values(["competition", "as_of_dt"]).reset_index(drop=True)
    m = matches.copy()
    m["_asof_dt"] = pd.to_datetime(m["date"])

    out = m
    for side, team_col in (("h", "home_team_id"), ("a", "away_team_id")):
        sub = m[["_asof_dt", "competition", team_col, "game_id"]].copy()
        sub = sub.rename(columns={team_col: "team_id"})
        # nearest previous rating with no exact match on the same date
        merged = pd.merge_asof(
            sub.sort_values("_asof_dt"),
            to_join.rename(columns={"as_of_dt": "_asof_dt"}),
            by="competition", left_on="_asof_dt", right_on="_asof_dt",
            direction="backward", allow_exact_matches=False,
        )
        merged = merged.rename(columns={
            "elo": f"{side}_elo", "last_game": f"{side}_elo_last_game"})
        merged = merged[["game_id", "competition", f"{side}_elo",
                         f"{side}_elo_last_game"]]
        out = out.merge(merged, on=["game_id", "competition"], how="left")

    out = out.drop(columns=["_asof_dt"])
    # defensive: rating must never include the match itself
    for side in ("h", "a"):
        mask = pd.to_datetime(out[f"{side}_elo_last_game"], errors="coerce") >= \
            pd.to_datetime(out["date"], errors="coerce")
        out.loc[mask, f"{side}_elo"] = np.nan
    return out


def weekend_or_league_round(games: pd.DataFrame) -> pd.DataFrame:
    """Add a round guess (number of the league round) by counting previous
    league matches of the same season home team has played. Pure pre-match
    information (counts of past matches only)."""
    g = games.sort_values(["starts_at"]).copy()
    g.loc[:, "season_round"] = g.groupby("season").cumcount()  # rough slot
    return g