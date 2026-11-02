"""Tests: odds intake parsing + recommendation ranking/maths."""
import numpy as np
import pandas as pd
import pytest

from features.intake import (OddsIntake, TeamResolver, normalize_name,
                             parse_odds_text)


def test_normalize_name():
    assert normalize_name("Atlético de Madrid") == "atletico de madrid"
    assert normalize_name(" FC Barcelona ") == "fc barcelona"
    assert normalize_name("Real Madrid C.F.") == "real madrid c f"


def test_parse_odds_text_from_string(tmp_path):
    p = tmp_path / "odds.json"
    p.write_text('{"bookmaker": "w", "matches": [{"home": "A", "away": "B", '
                 '"markets": {"1x2": {"home": 2.0, "draw": 3.0, "away": 4.0}, '
                 '"over_under": {"line": 2.5, "over": 1.9, "under": 1.9}, '
                 '"btts": {"yes": 1.8, "no": 2.0}}}]}')
    data = parse_odds_text(str(p))
    assert data["bookmaker"] == "w"


def test_intake_flattens_markets():
    payload = {"matches": [{"home": "A", "away": "B", "markets": {
        "1x2": {"home": 2.0, "draw": 3.0, "away": 4.0},
        "over_under": {"line": 2.5, "over": 1.9, "under": 1.9},
        "btts": {"yes": 1.8, "no": 2.0}}}]}
    intake = OddsIntake(payload)
    assert len(intake.rows) == 7  # 3 + 2 + 2
    assert set(intake.rows["market"]) == {"1x2", "over_under", "btts"}


def test_intake_rejects_unknown_market():
    payload = {"matches": [{"home": "A", "away": "B",
                            "markets": {"cards": {"over": 2.0}}}]}
    with pytest.raises(ValueError):
        OddsIntake(payload)


def test_team_resolver():
    teams = pd.DataFrame([
        {"team_id": "1", "name": "Atlético de Madrid", "abbrev": "ATM"},
        {"team_id": "2", "name": "FC Barcelona", "abbrev": "BAR"},
    ])
    r = TeamResolver(teams)
    assert r.resolve("Atletico de Madrid") == "1"
    assert r.resolve("BARCELONA") == "2"
    assert r.resolve("Equipo Fantasma") is None


class FakePoisson:
    """Poisson stub whose score markets are readable from team ids."""
    def market_probs(self, home, away, total_line=2.5):
        h = float(home[1:])
        a = float(away[1:])
        p_home = 0.5 + (h - a) / 20
        p_draw = 0.25
        p_away = 1 - p_home - p_draw
        p_over = 0.55
        p_btts = 0.60
        lam = {"h": h, "a": a}
        return {"p_home": p_home, "p_draw": p_draw, "p_away": p_away,
                "p_over": p_over, "p_under": 1 - p_over,
                "p_btts": p_btts,
                "lambda_home": lam["h"], "lambda_away": lam["a"]}


def test_recommend_scoring_and_ranking():
    from features.config import FeatureConfig
    from features.recommend import Recommender
    cfg = FeatureConfig(competition="demo")
    rec = Recommender(cfg, FakePoisson(), gbm=None)

    teams = pd.DataFrame([
        {"team_id": "T1", "name": "Team One", "abbrev": "T1"},
        {"team_id": "T2", "name": "Team Two", "abbrev": "T2"},
        {"team_id": "T3", "name": "Team Three", "abbrev": "T3"},
    ])
    resolver = TeamResolver(teams)

    payload = {"matches": [
        {"home": "Team One", "away": "Team Two", "markets": {
            "1x2": {"home": 2.4, "draw": 3.5, "away": 2.9},
            "btts": {"yes": 2.5, "no": 1.6}}},
        {"home": "Team Three", "away": "Team One", "markets": {
            "1x2": {"home": 3.0, "draw": 3.1, "away": 2.2}}},

    ]}
    intake = OddsIntake(payload)
    scored = rec.score(intake, resolver)

    # EV arithmetic: EV = p_model * odds - 1
    row = scored[(scored["home_raw"] == "Team One") &
                 (scored["selection"] == "home")].iloc[0]
    assert row["ev"] == pytest.approx(row["p_model"] * row["odds_dec"] - 1)
    assert row["p_breakeven"] == pytest.approx(1 / row["odds_dec"])

    # unknown teams are excluded
    payload2 = {"matches": [{"home": "Equipo X", "away": "Team One",
                             "markets": {"1x2": {"home": 2.0}}}]}
    s2 = rec.score(OddsIntake(payload2), resolver)
    assert s2.empty

    # odds window filter + ev floor
    picks = rec.recommend(scored, target_odds=2.5, tolerance=0.15, min_ev=0.0,
                          criterion="value", top=3)
    assert all(abs(p["odds_dec"] - 2.5) <= 0.15 for _, p in picks.iterrows())
    assert all(p["ev"] >= 0.0 for _, p in picks.iterrows())

    # safety criterion ranks by p_model
    picks_safety = rec.recommend(scored, target_odds=2.5, tolerance=0.5,
                                 min_ev=0.0, criterion="safety", top=5)
    assert picks_safety["p_model"].is_monotonic_decreasing


def test_recommend_empty_when_no_value():
    from features.config import FeatureConfig
    from features.recommend import Recommender
    cfg = FeatureConfig(competition="demo")
    rec = Recommender(cfg, FakePoisson(), gbm=None)
    teams = pd.DataFrame([{"team_id": "T1", "name": "A", "abbrev": "A"},
                          {"team_id": "T2", "name": "B", "abbrev": "B"}])
    resolver = TeamResolver(teams)
    payload = {"matches": [{"home": "A", "away": "B",
                            "markets": {"1x2": {"home": 1.2}}}]}
    scored = rec.score(OddsIntake(payload), resolver)
    picks = rec.recommend(scored, target_odds=2.5, tolerance=0.2, min_ev=0.0)
    assert picks.empty