"""Recommender: model probabilities × user odds → ranked picks for a request
like "una cuota alrededor de 2.5, lo más segura que la veas".

DECISION_D26 — The recommendation is a CONSTRAINED RANKING, not a bet order:
  - Filter: selections whose decimal odds are within ``tolerance`` of the
    requested target (e.g. 2.5 ± 0.15), and whose model EV >= ``min_ev``.
  - Rank: by EV (default, "value") or by model probability ("safety").
  - Every row reports BOTH so the human sees the trade-off. A 2.5 odds pick
    needs p >= 0.40 just to break even; "safe" and "odds 2.5" pull in opposite
    directions and the table shows it.

DECISION_D27 — Candidate markets come from the score matrix (Poisson):
  1X2 via the blend (or Poisson alone without tabular features); Over/Under
  evaluated at the book's quoted line; BTTS from score space. Handicaps are
  left for a later phase (needs scored matrix at the book's handicap line).

DECISION_D28 — Unknown teams (fixture not in our training universe, e.g.
  a friendly or a cup tie) are excluded from the input, not guessed. We refuse
  to recommend what the model cannot rate.

DECISION_D29 — EV uses raw un-normalised user odds (what they can actually
  bet at), while the model probabilities must ideally be calibrated. Until
  calibration lands, the default EV threshold is 0 and the report flags picks
  whose edge is below the margin of the market (implicit in odds total).

DECISION_D30 — A pick's "required probability" (breakeven = 1/odds) is shown
  next to the model probability: this makes explicit why high odds can't be
  "safe" and helps avoid the gambler's temptation of long shots.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from features.config import FeatureConfig
from features.intake import OddsIntake, TeamResolver
from features.models import Ensemble, GBMResultModel, PoissonGoalsModel


class Recommender:
    def __init__(self, cfg: FeatureConfig, poisson: PoissonGoalsModel,
                 gbm: GBMResultModel | None = None):
        self.cfg = cfg
        self.poisson = poisson
        self.ens = Ensemble(poisson, gbm, weight=cfg.poisson_weight)

    # --------------------------------------------------------------- scoring
    def score(self, intake: OddsIntake, resolver: TeamResolver) -> pd.DataFrame:
        """Attach model probabilities and EV to every pasted selection."""
        rows = intake.rows.copy()
        scored = []
        for _, r in rows.iterrows():
            home_id = resolver.resolve(r["home_raw"])
            away_id = resolver.resolve(r["away_raw"])
            if home_id is None or away_id is None:
                continue  # D28: no recommendation for unknown teams
            if r["market"] == "1x2":
                mp = self.poisson.market_probs(home_id, away_id)
                p = {"home": mp["p_home"], "draw": mp["p_draw"],
                     "away": mp["p_away"]}[r["selection"]]
                # blend correction when tabular model exists
                if self.ens.gbm is not None:
                    fx = pd.DataFrame([{
                        "home_team_id": home_id, "away_team_id": away_id}])
                    p_blend = self.ens.predict_proba_1x2(fx).ravel()
                    p = {"home": p_blend[0], "draw": p_blend[1],
                         "away": p_blend[2]}[r["selection"]]
            elif r["market"] == "over_under":
                line = float(r["line"]) if pd.notna(r.get("line")) else 2.5
                mp = self.poisson.market_probs(home_id, away_id, total_line=line)
                p = mp["p_over"] if r["selection"] == "over" else mp["p_under"]
            elif r["market"] == "btts":
                mp = self.poisson.market_probs(home_id, away_id)
                p = mp["p_btts"] if r["selection"] == "yes" else 1 - mp["p_btts"]
            else:
                continue
            odds = float(r["odds_dec"])
            rec = dict(r)
            rec["home_id"], rec["away_id"] = home_id, away_id
            rec["p_model"] = float(p)
            rec["p_breakeven"] = 1.0 / odds if odds > 0 else np.nan
            rec["ev"] = p * odds - 1.0
            scored.append(rec)
        return pd.DataFrame(scored)

    # -------------------------------------------------------- recommendation
    def recommend(self, scored: pd.DataFrame, target_odds: float = 2.5,
                  tolerance: float = 0.15, min_ev: float = 0.0,
                  criterion: str = "value", top: int = 5,
                  single_fixture_limit: bool = False) -> pd.DataFrame:
        """Filter by odds window + min EV, rank by criterion.

        ``single_fixture_limit`` (default False) keeps one recommendation per
        fixture when True (to avoid 5 picks from the same match crowding the
        shortlist) — useful for "dame la mejor de cada partido".
        """
        s = scored.copy()
        candidates = s[np.abs(s["odds_dec"] - target_odds) <= tolerance]
        candidates = candidates[candidates["ev"] >= min_ev]

        if single_fixture_limit and len(candidates):
            rank_col = "ev" if criterion == "value" else "p_model"
            idx = (candidates.sort_values(rank_col, ascending=False)
                   .groupby("fixture_idx").head(1).index)
            candidates = candidates.loc[idx]

        candidates = candidates.copy()
        # DECISION_D26: "value" ordena por EV; "safety" por probabilidad del
        # modelo (todas las filas comparten ventana de cuotas, así ambas son
        # comparables dentro del filtro).
        candidates["score"] = (candidates["ev"] if criterion == "value"
                               else candidates["p_model"])
        candidates = candidates.sort_values("score", ascending=False).head(top)
        candidates["criterion"] = criterion
        return candidates

    # ------------------------------------------------------------------ out
    @staticmethod
    def render(candidates: pd.DataFrame) -> str:
        if candidates.empty:
            return ("Ninguna selección cumple los criterios "
                    "(probablemente las cuotas pedidas exigen una probabilidad "
                    "del modelo demasiado alta para ningún mercado).")
        cols = ["home_raw", "away_raw", "market", "selection", "line",
                "odds_dec", "p_model", "p_breakeven", "ev"]
        out = candidates[[c for c in cols if c in candidates.columns]].copy()
        out["ev"] = out["ev"].map(lambda v: f"{v:+.3f}")
        out["odds_dec"] = out["odds_dec"].map(lambda v: f"{v:.2f}")
        out["p_model"] = out["p_model"].map(lambda v: f"{v:.3f}")
        out["p_breakeven"] = out["p_breakeven"].map(
            lambda v: f"{v:.3f}" if pd.notna(v) else "")
        lines = [f"Criterio: {'EV (value)' if candidates.iloc[0]['criterion'] == 'value' else 'probabilidad (safety)'}"]
        lines.append(out.to_string(index=False))
        lines.append("p_model = prob. del modelo | p_breakeven = 1/cuota | ev = p_model*cuota-1")
        return "\n".join(lines)