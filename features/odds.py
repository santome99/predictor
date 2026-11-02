"""Odds mathematics: American->decimal, margin removal, implied probabilities.

DECISION_D3 — Remove the bookmaker margin (overround) from quoted odds and
work with *no-margin* probabilities as features. Why:
  - Raw 1X2 odds sum of implied probabilities > 1 (the overround). Feeding
    that inflated sum into a model biases every probability upward.
  - Normalising (p_i / sum p) recovers the book's *true belief* approximately,
    which is the single strongest pre-match feature available.
  - When testing EV+ later, the un-normalised quote is still the number you
    can actually bet at, so both representations are kept (fair prob vs price).

DECISION_D4 — Aggregation across bookmakers uses the MEDIAN closed quote, not
the mean. Reasons: a single bad line (mistyped, suspended account, outlier
book) distorts the mean; the median is the consensus price across books and
is robust to one-off outliers. Per-book raw values remain untouched in
``odds_books``.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ingestion.prices import american_to_decimal


def american_col_to_decimal(s: pd.Series) -> pd.Series:
    """Vectorised American->decimal conversion; keeps NaN as NaN."""
    return s.map(lambda v: american_to_decimal(v) if pd.notna(v) else np.nan)


def implied_from_decimal(dec: pd.Series) -> pd.Series:
    """Implied probability = 1/decimal; NaN-preserving."""
    return 1.0 / dec.astype(float)


def remove_margin(probs: np.ndarray) -> np.ndarray:
    """Normalise a row of raw implied probabilities to sum to 1.

    overround = sum(probs) - 1; fair_i = prob_i / sum(probs).
    Rows with any missing value return all-NaN (cannot normalise partially).
    """
    probs = np.asarray(probs, dtype=float)
    if np.isnan(probs).any() or len(probs) == 0:
        return np.full_like(probs, np.nan)
    s = probs.sum()
    if s <= 0 or not np.isfinite(s):
        return np.full_like(probs, np.nan)
    return probs / s


def overround(dec: np.ndarray) -> float:
    """Overround = sum(1/dec) - 1. Diagnostic of how wide the margin is."""
    dec = np.asarray(dec, dtype=float)
    if np.isnan(dec).any() or len(dec) == 0:
        return np.nan
    return float((1.0 / dec).sum() - 1.0)


def _median_or_nan(s: pd.Series) -> float:
    """Median of a series, NaN if the series is empty or all-NaN."""
    vals = s.dropna()
    if len(vals) == 0:
        return np.nan
    return float(vals.median())


def game_1x2_features(books: pd.DataFrame) -> pd.DataFrame:
    """Per-book -> per-game 1X2 features.

    Input: odds_books rows for one game with columns:
      home_ml_close_dec, draw_ml_close_dec, away_ml_close_dec (decimal),
      and the same *_open_dec prefixed alternatively (open_*).
    Output: a 1-row DataFrame with median fair probabilities across books
      for closing and opening, plus median quoted decimal odds (price you
      can bet at) and number of books.
    """
    feats: dict = {}
    close_dec = books[["home_ml_close_dec", "draw_ml_close_dec", "away_ml_close_dec"]]
    n_priced = int(close_dec.notna().all(axis=1).astype(int).sum())
    feats["n_books"] = n_priced

    fair_rows = []
    for _, row in close_dec.iterrows():
        ok = bool(row.notna().all())
        if ok:
            fair_rows.append(remove_margin(implied_from_decimal(row).to_numpy()))
    if fair_rows:
        fair_all = np.nanmedian(np.vstack(fair_rows), axis=0)
        feats["p_home_close"] = float(fair_all[0])
        feats["p_draw_close"] = float(fair_all[1])
        feats["p_away_close"] = float(fair_all[2])
    else:
        feats["p_home_close"] = np.nan
        feats["p_draw_close"] = np.nan
        feats["p_away_close"] = np.nan

    # median quoted closing decimal prices (real bettable prices)
    feats["dec_home_close"] = _median_or_nan(close_dec["home_ml_close_dec"])
    feats["dec_draw_close"] = _median_or_nan(close_dec["draw_ml_close_dec"])
    feats["dec_away_close"] = _median_or_nan(close_dec["away_ml_close_dec"])

    # opening odds (if any book has them) -> fair probs and prices
    open_dec = books[["home_ml_open_dec", "draw_ml_open_dec", "away_ml_open_dec"]]
    open_rows = []
    for _, row in open_dec.iterrows():
        if bool(row.notna().all()):
            open_rows.append(remove_margin(implied_from_decimal(row).to_numpy()))
    if open_rows:
        fair_o = np.nanmedian(np.vstack(open_rows), axis=0)
        feats["p_home_open"] = float(fair_o[0])
        feats["p_draw_open"] = float(fair_o[1])
        feats["p_away_open"] = float(fair_o[2])
    else:
        feats["p_home_open"] = np.nan
        feats["p_draw_open"] = np.nan
        feats["p_away_open"] = np.nan
    feats["dec_home_open"] = _median_or_nan(open_dec["home_ml_open_dec"])
    feats["dec_draw_open"] = _median_or_nan(open_dec["draw_ml_open_dec"])
    feats["dec_away_open"] = _median_or_nan(open_dec["away_ml_open_dec"])

    return pd.DataFrame([feats])


def game_totals_features(books: pd.DataFrame) -> pd.DataFrame:
    """Per-book -> per-game totals (Over/Under) features.

    DECISION_D5 — The Over/Under market is the pair (line, price) per book.
    We keep the median *line* across books and the median no-margin
    probability of the over at each book's own line, so ``p_over_close`` is
    always relative to the market line, never a fixed global 2.5.

    NOTE: the API only quotes over/under prices on the CLOSING total (it has
    no separate opening over/under pair in ``get_game.odds``), so opening
    analysis for totals is limited to the line itself (``total_open``).
    """
    feats: dict = {}
    lines = []
    over_fairs, under_fairs = [], []
    for _, row in books.iterrows():
        if pd.notna(row.get("total_close")) and pd.notna(row.get("over_odds_dec")) \
                and pd.notna(row.get("under_odds_dec")):
            lines.append(row["total_close"])
            p_raw = implied_from_decimal(pd.Series(
                [row["over_odds_dec"], row["under_odds_dec"]])).to_numpy()
            fair = remove_margin(p_raw)
            if not np.isnan(fair).any():
                over_fairs.append(fair[0])
                under_fairs.append(fair[1])
    if lines:
        feats["total_line_close"] = float(np.nanmedian(lines))
        feats["p_over_close"] = float(np.nanmedian(over_fairs)) if over_fairs else np.nan
        feats["p_under_close"] = float(np.nanmedian(under_fairs)) if under_fairs else np.nan
    else:
        feats["total_line_close"] = np.nan
        feats["p_over_close"] = np.nan
        feats["p_under_close"] = np.nan

    lines_o = [row["total_open"] for _, row in books.iterrows()
               if pd.notna(row.get("total_open"))]
    feats["total_line_open"] = float(np.nanmedian(lines_o)) if lines_o else np.nan
    return pd.DataFrame([feats])


def test_overround_free():
    """Sanity: a fair book has overround 0."""
    assert overround(np.array([2.0, 3.0, 6.0])) == 0.0