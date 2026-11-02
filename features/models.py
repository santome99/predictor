"""Models: bivariate Poisson (goals) + histogram GBM (1X2) + calibrated blend.

DECISION_D13 — Separate **score** (Poisson) from **result** (GBM) models.
  - Poisson is generative: it models the goal count distribution of each team
    (lambda_home, lambda_away) and lets ANY derivative market be read off the
    score matrix: 1X2, over/under any line, BTTS, handicaps. It needs few
    parameters and is robust to small data — ideal as backbone.
  - The tabular GBM captures non-linear interactions among contextual features
    (form, rest, Elo, market signal) that Poisson attack/defense ignores.
  - Their average (weight ``poisson_weight``) is the final 1X2 probability;
    the Poisson remains the sole source for score space (lines, BTTS).

DECISION_D14 — Poisson MLE with attack/defense decomposition per team plus
  home advantage, scoring matrix truncated at ``max_goals=8``. Truncation
  (renormalising rows) keeps the matrix finite; 8 goals covers virtually all
  league scorelines. Fit via full-batch log-likelihood maximisation (scipy
  L-BFGS): convex enough for these sizes, no need for minibatches.

DECISION_D15 — sklearn ``HistGradientBoostingClassifier`` as the tabular
  learner. Rationale: it is scikit-learn's built-in LightGBM-equivalent
  (histogram binning), supports NaN features natively (matches with no odds
  or missing Elo still train), and avoids an extra heavy dependency in the
  homelab env (no xgboost/lightgbm wheels needed). Swapping later to
  real LightGBM only requires changing the estimator, not the pipeline.
"""
from __future__ import annotations

import itertools
import json
from typing import Optional

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson
from sklearn.ensemble import HistGradientBoostingClassifier

from features.config import FeatureConfig

X_DROP = ["game_id", "date", "starts_at", "frozen_at", "status", "stage",
          "venue", "city", "result", "home_score", "away_score", "season",
          "competition", "home_team_id", "away_team_id"]


# ------------------------------------------------------------------ Poisson
class PoissonGoalsModel:
    """Bivariate Poisson with team attack/defense + home advantage."""

    def __init__(self, max_goals: int = 8):
        self.max_goals = max_goals
        self.teams_: Optional[list] = None
        self.team_index_: Optional[dict] = None
        self.beta_home_: float = 0.0
        self.attack_: Optional[np.ndarray] = None
        self.defense_: Optional[np.ndarray] = None

    @staticmethod
    def _design(matches: pd.DataFrame, teams: list, team_index: dict):
        """Return A_home, A_away (M x (1+2T)): row = [1][attack_home][defense_away]."""
        M = len(matches)
        A_home = np.zeros((M, 1 + 2 * len(teams)))
        A_away = np.zeros((M, 1 + 2 * len(teams)))
        for i, (_, row) in enumerate(matches.iterrows()):
            h, a = row["home_team_id"], row["away_team_id"]
            A_home[i, 0] = 1.0
            A_away[i, 0] = 0.0
            if h in team_index:
                A_home[i, 1 + team_index[h]] = 1.0
            if a in team_index:
                A_home[i, 1 + len(teams) + team_index[a]] = 1.0
            if a in team_index:
                A_away[i, 1 + team_index[a]] = 1.0
            if h in team_index:
                A_away[i, 1 + len(teams) + team_index[h]] = 1.0
        return A_home, A_away

    def fit(self, matches: pd.DataFrame, reg: float = 1e-4):
        """matches needs home_team_id, away_team_id, home_score, away_score."""
        teams = sorted(set(matches["home_team_id"]) | set(matches["away_team_id"]))
        self.teams_ = teams
        self.team_index_ = {t: i for i, t in enumerate(teams)}
        A_h, A_a = self._design(matches, teams, self.team_index_)
        y_h = matches["home_score"].to_numpy().astype(float)
        y_a = matches["away_score"].to_numpy().astype(float)
        p0 = np.zeros(1 + 2 * len(teams))
        p0[0] = 0.25  # home advantage prior

        def neg_ll(theta):
            lam_h = np.exp(np.clip(A_h @ theta, -10, 10))
            lam_a = np.exp(np.clip(A_a @ theta, -10, 10))
            ll = (poisson.logpmf(y_h, lam_h) + poisson.logpmf(y_a, lam_a)).sum()
            reg_pen = reg * (theta[1:] @ theta[1:] + (theta[0] - 0.25) ** 2)
            return -ll + reg_pen

        res = minimize(neg_ll, p0, method="L-BFGS-B",
                       options={"maxiter": 2000, "ftol": 1e-10})
        theta = res.x
        self.beta_home_ = float(theta[0])
        self.attack_ = theta[1:1 + len(teams)]
        self.defense_ = theta[1 + len(teams):]
        return self

    def rates(self, home_id: str, away_id: str) -> tuple:
        def val(arr, key):
            idx = self.team_index_.get(key)
            return float(arr[idx]) if idx is not None else 0.0
        lam_h = np.exp(self.beta_home_ + val(self.attack_, home_id) +
                       val(self.defense_, away_id))
        lam_a = np.exp(val(self.attack_, away_id) + val(self.defense_, home_id))
        return lam_h, lam_a

    def score_matrix(self, home_id: str, away_id: str) -> np.ndarray:
        lam_h, lam_a = self.rates(home_id, away_id)
        m = min(self.max_goals, 12)
        ph = poisson.pmf(np.arange(m), lam_h)
        pa = poisson.pmf(np.arange(m), lam_a)
        mx = np.outer(ph, pa)
        return mx / mx.sum()

    def market_probs(self, home_id: str, away_id: str, total_line: float = 2.5):
        mx = self.score_matrix(home_id, away_id)
        m = mx.shape[0]
        p_home = float(np.tril(mx, -1).sum())
        p_away = float(np.triu(mx, 1).sum())
        p_draw = float(np.trace(mx))
        rows, cols = np.meshgrid(np.arange(m), np.arange(m), indexing="ij")
        p_over = float(mx[rows + cols > total_line].sum())
        p_under = float(mx[rows + cols <= total_line].sum())
        p_btts = float(mx[(rows >= 1) & (cols >= 1)].sum())
        return {
            "p_home": p_home, "p_draw": p_draw, "p_away": p_away,
            "p_over": p_over, "p_under": p_under, "p_btts": p_btts,
            "lambda_home": float(self.rates(home_id, away_id)[0]),
            "lambda_away": float(self.rates(home_id, away_id)[1]),
        }

    def save(self, path):
        path.write_text(json.dumps({
            "teams": self.teams_, "beta_home": self.beta_home_,
            "attack": self.attack_.tolist(), "defense": self.defense_.tolist(),
            "max_goals": self.max_goals,
        }))

    @classmethod
    def load(cls, path):
        d = json.loads(path.read_text())
        obj = cls(max_goals=d["max_goals"])
        obj.teams_ = d["teams"]
        obj.team_index_ = {t: i for i, t in enumerate(d["teams"])}
        obj.beta_home_ = d["beta_home"]
        obj.attack_ = np.array(d["attack"])
        obj.defense_ = np.array(d["defense"])
        return obj


# -------------------------------------------------------------- GBM 1X2
class GBMResultModel:
    """Multiclass 1X2 model over tabular features. Supports NaN features."""

    def __init__(self, cfg: FeatureConfig):
        self.cfg = cfg
        self.model = HistGradientBoostingClassifier(
            loss="log_loss", max_iter=cfg.gbm_max_iter,
            learning_rate=cfg.gbm_learning_rate,
            max_leaf_nodes=cfg.gbm_max_leaf_nodes,
            min_samples_leaf=20, l2_regularization=1.0,
            early_stopping=False, random_state=42)
        self.feature_cols_: Optional[list] = None

    @staticmethod
    def _target_class(row: pd.Series) -> int:
        if row["home_win"] == 1:
            return 0
        if row["draw_game"] == 1:
            return 1
        return 2

    def fit(self, matrix: pd.DataFrame):
        feats = [c for c in matrix.columns if c not in X_DROP]
        feats = [c for c in feats
                 if (c.startswith(("h_", "a_", "p_", "dec_", "n_books",
                                   "total_", "lag_", "rest_", "season_"))
                     and not c.endswith(("_last_game", "_id")))]
        y = np.array([self._target_class(r) for _, r in matrix.iterrows()])
        self.feature_cols_ = feats
        self.model.fit(matrix[feats].astype(float), y)
        return self

    def predict_proba(self, matrix: pd.DataFrame) -> np.ndarray:
        """Columns: [home, draw, away].

        DECISION_D21bis — prediction rows must be aligned to the training
        feature list column-by-column; any feature missing at prediction time
        (e.g., odds not yet posted for a fixture, Elo absent) is filled with
        NaN. HistGradientBoosting handles NaN natively by treating it as its
        own branch direction, so missing market data never breaks inference.
        """
        feats = self.feature_cols_ or [c for c in matrix.columns
                                       if c not in X_DROP]
        X = matrix.reindex(columns=feats).astype(float)
        return self.model.predict_proba(X)


class Ensemble:
    """Weighted blend of Poisson and GBM 1X2 probabilities."""

    def __init__(self, poisson_model: PoissonGoalsModel,
                 gbm: Optional[GBMResultModel], weight: float = 0.5):
        self.poisson = poisson_model
        self.gbm = gbm
        self.w = weight

    def predict_proba_1x2(self, matrix: pd.DataFrame) -> np.ndarray:
        p_pois = []
        for _, row in matrix.iterrows():
            mp = self.poisson.market_probs(str(row["home_team_id"]),
                                           str(row["away_team_id"]))
            p_pois.append([mp["p_home"], mp["p_draw"], mp["p_away"]])
        p_pois = np.array(p_pois)
        if self.gbm is not None:
            p_gbm = self.gbm.predict_proba(matrix)
            return self.w * p_pois + (1 - self.w) * p_gbm
        return p_pois

    def predict_score_market(self, matrix: pd.DataFrame,
                             lines=(2.5,)) -> pd.DataFrame:
        """Score-space markets from Poeason alone (lines, BTTS, totals)."""
        rows = []
        for _, r in matrix.iterrows():
            mp = self.poisson.market_probs(str(r["home_team_id"]),
                                           str(r["away_team_id"]))
            entry = {"game_id": r["game_id"],
                     "lambda_home": mp["lambda_home"],
                     "lambda_away": mp["lambda_away"],
                     "p_btts": mp["p_btts"]}
            for line in lines:
                on = self.poisson.market_probs(
                    str(r["home_team_id"]), str(r["away_team_id"]), total_line=line)
                entry[f"p_over_{str(line).replace('.', '_')}"] = on["p_over"]
            rows.append(entry)
        return pd.DataFrame(rows)