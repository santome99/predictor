"""Walk-forward backtest: honest temporal evaluation + EV/ROI vs closing odds.

DECISION_D16 — Walk-forward, never shuffled cross-validation. Football data is
a time series: a random CV split lets the model "see" both sides of a season
swap (or a team's fortune later in time), inflating scores. We always train on
matches strictly BEFORE the test block and evaluate chronologically.

DECISION_D17 — Expanding window by season. Train on all completed seasons < T,
test on season T. This mirrors live deployment (you bet with everything you
know until Friday and settle the weekend) and makes the backtest a dry-run of
production.

DECISION_D18 — EV and ROI are measured only against CLOSING odds: the closing
line is the market's best estimate at the moment closest to kick-off, so
beating it is the strictest honest test. Opening odds are also reported
(beating opening is easier and more exploitable in practice: early birds).

DECISION_D19 — No bet on outcomes with EV <= threshold (default 0). The
threshold encodes the profit-margin required to overcome variance. P/L:
win -> +dec-1 per unit, loss -> -1. ROI = net stake-weighted return.
"""
from __future__ import annotations

import json
from typing import Optional

import numpy as np
import pandas as pd

from features.config import FeatureConfig
from features.models import Ensemble, GBMResultModel, PoissonGoalsModel

MARKETS = ["home", "draw", "away"]


def _logloss(y_true: np.ndarray, p_pred: np.ndarray, eps: float = 1e-12):
    p = np.clip(p_pred, eps, 1 - eps)
    return float(-np.mean(np.log(p[np.arange(len(y_true)), y_true])))


def _brier(probs: np.ndarray, market: str, df: pd.DataFrame) -> dict:
    """Brier score for one market ('home'|'draw'|'away')."""
    col = {"home": "home_win", "draw": "draw_game", "away": "away_win"}[market]
    y = df[col].astype(float).to_numpy()
    p = probs[:, MARKETS.index(market)]
    return {"mean": float(np.mean((p - y) ** 2))}


def evaluate_block(train: pd.DataFrame, test: pd.DataFrame,
                   cfg: FeatureConfig) -> dict:
    """Fit on train (season < T), predict test (season T), return metrics +
    bet settlement using closing decimal odds as available prices."""
    poisson = PoissonGoalsModel(max_goals=cfg.max_goals).fit(train)
    gbm = None
    if len(train) >= cfg.min_train_matches and len(train.columns) > 1:
        try:
            gbm = GBMResultModel(cfg).fit(train)
        except Exception:
            gbm = None
    ens = Ensemble(poisson, gbm, weight=cfg.poisson_weight)
    probs = ens.predict_proba_1x2(test)

    y_true = np.zeros(len(test), dtype=int)
    y_true[test["draw_game"] == 1] = 1
    y_true[test["away_win"] == 1] = 2
    metrics = {
        "n_train": int(len(train)), "n_test": int(len(test)),
        "logloss_1x2": _logloss(y_true, probs),
        "accuracy": float((probs.argmax(1) == y_true).mean()),
    }
    for col, mk in zip(("home_win", "draw_game", "away_win"), MARKETS):
        metrics[f"brier_{mk}"] = _brier(probs, mk, test)["mean"]

    # ---- betting: EV against closing & opening prices
    for side, prob_i, dec_col, label in (
        (0, 0, "dec_home_close", "close"), (1, 1, "dec_draw_close", "close"),
        (2, 2, "dec_away_close", "close"), (0, 0, "dec_home_open", "open"),
        (1, 1, "dec_draw_open", "open"), (2, 2, "dec_away_open", "open")):
        p = probs[:, prob_i]
        dec = test[dec_col].to_numpy() if dec_col in test.columns else np.full(len(test), np.nan)
        ev = p * dec - 1.0
        bet = ev > cfg.ev_threshold
        settle = np.where(y_true == side, dec - 1.0, -1.0)
        valid = bet & ~np.isnan(dec) & pd.notna(test[dec_col].to_numpy())
        n = int(valid.sum())
        if n:
            pl = float((settle[valid] * cfg.bet_stake).sum())
            roi = float(pl / (n * cfg.bet_stake))
        else:
            pl, roi = 0.0, np.nan
        metrics[f"bets_{MARKETS[side]}_{label}"] = n
        metrics[f"pl_{MARKETS[side]}_{label}"] = pl
        metrics[f"roi_{MARKETS[side]}_{label}"] = roi

    metrics["season_test"] = int(test["season"].iloc[0]) if len(test) else None
    return metrics


def walk_forward(matrix: pd.DataFrame, cfg: FeatureConfig,
                 min_seasons_train: int = 1) -> pd.DataFrame:
    """Season-expanding evaluation. Returns a row per test season."""
    mat = matrix.sort_values("starts_at").reset_index(drop=True)
    seasons = sorted(mat["season"].dropna().unique().tolist())
    rows = []
    if len(seasons) <= min_seasons_train + 1:
        # insufficient structure: split 80/20 chronologically anyway
        cut = int(len(mat) * 0.8)
        if cut < cfg.min_train_matches or len(mat) - cut < 20:
            return pd.DataFrame()
        m = evaluate_block(mat.iloc[:cut], mat.iloc[cut:], cfg)
        rows.append(m)
        return pd.DataFrame(rows)

    for t in sorted(seasons[min_seasons_train:]):
        train = mat.loc[mat["season"] < t].copy()
        test = mat.loc[mat["season"] == t].copy()
        if len(train) < 5 or len(test) == 0:
            continue
        rows.append(evaluate_block(train, test, cfg))
    return pd.DataFrame(rows)


def summarize(wf: pd.DataFrame) -> dict:
    if wf.empty:
        return {"ok": False, "seasons": 0}
    agg = {
        "ok": True,
        "seasons": int(len(wf)),
        "avg_logloss": float(wf["logloss_1x2"].mean()),
        "avg_accuracy": float(wf["accuracy"].mean()),
        "bets_close": int(wf[[c for c in wf.columns
                              if c.startswith("bets_") and c.endswith("_close")]]
                          .sum().sum()),
        "pl_close": float(wf[[c for c in wf.columns
                              if c.startswith("pl_") and c.endswith("_close")]]
                          .sum().sum()),
        "pl_open": float(wf[[c for c in wf.columns
                             if c.startswith("pl_") and c.endswith("_open")]]
                         .sum().sum()),
    }
    stakes_close = agg["bets_close"] * 1.0
    agg["roi_close"] = (agg["pl_close"] / stakes_close
                        if stakes_close > 0 else np.nan)
    return agg


def run_backtest(matrix: pd.DataFrame, cfg: FeatureConfig,
                 out_path) -> dict:
    wf = walk_forward(matrix, cfg)
    summary = summarize(wf)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wf.to_parquet(out_path, index=False)
    with open(out_path.with_suffix(".summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    return summary