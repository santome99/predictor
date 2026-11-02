"""Synthetic demo dataset: 12 teams, 4 seasons × double round-robin.

DECISION_D24 — The demo generator is a controlled sandbox: it builds a tiny
but complete DuckDB (games, team stats, odds, Elo) with a KNOWN generative
model so learners can verify the pipeline recovers the built-in strengths.
It is deterministic (fixed seed). It must NEVER be mixed with real data:
it writes only to the given path and uses clearly synthetic team ids.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

TEAMS = [f"D{i:02d}" for i in range(12)]
# hidden true strengths: attack, defense
STRENGTH = {t: (np.random.default_rng(7 + i).normal(0.15, 0.35),
                np.random.default_rng(7 + i).normal(0.0, 0.3))
            for i, t in enumerate(TEAMS)}

N_SEASONS = 4
START_YEAR = 2022

rng = np.random.default_rng(42)


def _gen_season(season: int) -> pd.DataFrame:
    """Double round-robin (home & away) with Poisson draws."""
    rows = []
    day = dt.datetime(season, 8, 15, 19, 0)
    matches = []
    for h in TEAMS:
        for a in TEAMS:
            if h == a:
                continue
            matches.append((h, a))
    for i, (h, a) in enumerate(matches):
        ah, dh = STRENGTH[h]
        aa, da = STRENGTH[a]
        lam_h = np.exp(0.30 + ah - da)      # home advantage ~0.3
        lam_a = np.exp(aa - dh)
        hs = int(rng.poisson(lam_h))
        as_ = int(rng.poisson(lam_a))
        result = "H" if hs > as_ else ("A" if hs < as_ else "D")
        rows.append({
            "competition": "demo", "game_id": f"{season}-{i:03d}",
            "season": season, "stage": "Regular Season",
            "date": (day + dt.timedelta(days=i // 4)).strftime("%Y-%m-%d"),
            "starts_at": (day + dt.timedelta(days=i // 4) +
                          dt.timedelta(hours=i % 4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "final", "result": result,
            "home_team_id": h, "away_team_id": a,
            "home_score": hs, "away_score": as_,
            "venue": f"Stadium {h}", "city": "DemoCity",
        })
    return pd.DataFrame(rows)


def _gen_fixtures(season: int) -> pd.DataFrame:
    """One future round (scheduled, scores None) for the predict command."""
    rows = []
    day = dt.datetime.utcnow() + dt.timedelta(days=7)
    for i, h in enumerate(TEAMS):
        a = TEAMS[(i + 5) % len(TEAMS)]
        if a == h:
            a = TEAMS[(i + 6) % len(TEAMS)]
        rows.append({
            "competition": "demo", "game_id": f"{season}-fx{i:03d}",
            "season": season, "stage": "Regular Season",
            "date": day.strftime("%Y-%m-%d"),
            "starts_at": (day + dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "scheduled", "result": None,
            "home_team_id": h, "away_team_id": a,
            "home_score": None, "away_score": None,
            "venue": f"Stadium {h}", "city": "DemoCity",
        })
    return pd.DataFrame(rows)


def _team_stats(games: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, g in games.iterrows():
        for side, team, gf, ga in (("home", g["home_team_id"], g["home_score"], g["away_score"]),
                                   ("away", g["away_team_id"], g["away_score"], g["home_score"])):
            atk, deff = STRENGTH[team]
            lam = np.exp(atk - (deff) + (0.3 if side == "home" else 0))
            shots = int(max(1, rng.poisson(8 + 4 * lam)))
            sot = int(np.clip(rng.poisson(shots * 0.35), 1, shots))
            rows.append({
                "competition": "demo", "game_id": g["game_id"], "team_id": team,
                "home": side == "home", "goals": gf,
                "xg": round(lam + rng.normal(0, 0.2), 2),
                "possession_pct": round(np.clip(rng.normal(50 + 6 * lam, 8), 25, 75), 1),
                "shots": shots, "shots_on_target": sot,
                "blocked_shots": int(rng.poisson(2)), "corners": int(rng.poisson(5)),
                "fouls": int(rng.poisson(10)), "yellow_cards": int(rng.poisson(2)),
                "red_cards": int(rng.poisson(0.15)), "offsides": int(rng.poisson(2)),
                "saves": int(rng.poisson(3)), "passes": int(rng.normal(420, 60)),
                "accurate_passes": int(rng.normal(338, 50)),
                "crosses": int(rng.poisson(14)), "accurate_crosses": int(rng.poisson(3)),
                "long_balls": int(rng.poisson(44)), "accurate_long_balls": int(rng.poisson(21)),
                "tackles": int(rng.poisson(13)), "effective_tackles": int(rng.poisson(9)),
                "interceptions": int(rng.poisson(8)), "clearances": int(rng.poisson(18)),
                "penalty_goals": 0, "penalty_attempts": 0,
            })
    return pd.DataFrame(rows)


def _fair_probs(lam_h: float, lam_a: float, maxg: int = 12):
    """Fair 1X2 probabilities from the truncated bivariate Poisson.

    maxg=12: the demo strengths produce lambdas up to ~4; truncating at 8
    would drop too much probability mass. Uses the proper Poisson pmf
    (lambda**k * e**-lambda / k!) - the k! factorial is NOT optional.
    """
    from scipy.stats import poisson as _pois
    k = np.arange(maxg)
    ph = _pois.pmf(k, lam_h)
    ph /= ph.sum()
    pa = _pois.pmf(k, lam_a)
    pa /= pa.sum()
    mx = np.outer(ph, pa)
    mx /= mx.sum()
    p_home = float(np.tril(mx, -1).sum())
    p_away = float(np.triu(mx, 1).sum())
    p_draw = float(np.trace(mx))
    return p_home, p_draw, p_away


def _odds(games: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, g in games.iterrows():
        ah, dh = STRENGTH[g["home_team_id"]]
        aa, da = STRENGTH[g["away_team_id"]]
        lam_h = np.exp(0.30 + ah - da)
        lam_a = np.exp(aa - dh)
        p_h, p_d, p_a = _fair_probs(lam_h, lam_a)
        for b in ("houseA", "houseB"):
            margin = float(rng.normal(0.05, 0.008))  # ~5% overround
            def dec_of(p):
                return round(max(1.03, 1 / (p * (1 + margin))), 3)
            def am(p):
                dec = dec_of(max(p, 1e-6))
                return int(round((dec - 1) * 100)) if dec >= 2 else int(round(-100 / (dec - 1)))
            p_over = float(0.5)
            rows.append({
                "competition": "demo", "game_id": g["game_id"], "provider": b,
                "source": "archive",
                "home_ml_open_orig": None, "home_ml_open_dec": None,
                "draw_ml_open_orig": None, "draw_ml_open_dec": None,
                "away_ml_open_orig": None, "away_ml_open_dec": None,
                "home_ml_close_orig": am(p_h), "home_ml_close_dec": dec_of(p_h),
                "draw_ml_close_orig": am(p_d), "draw_ml_close_dec": dec_of(p_d),
                "away_ml_close_orig": am(p_a), "away_ml_close_dec": dec_of(p_a),
                "home_handicap_open": None, "home_handicap_close": None,
                "handicap_home_odds_orig": None, "handicap_home_odds_dec": None,
                "handicap_away_odds_orig": None, "handicap_away_odds_dec": None,
                "total_open": None, "total_close": 2.5,
                "over_odds_orig": am(p_over), "over_odds_dec": dec_of(p_over),
                "under_odds_orig": am(p_over), "under_odds_dec": dec_of(p_over),
                "close_prob_raw": None, "captured_at": None,
            })
    return pd.DataFrame(rows)


def _elo(games: pd.DataFrame) -> pd.DataFrame:
    # simple Elo simulation to give the as-of join something real
    elo = {t: 1500.0 for t in TEAMS}
    rows = []
    seen = set()
    for _, g in games.iterrows():
        k = 20
        eh, ea = elo[g["home_team_id"]], elo[g["away_team_id"]]
        exp_h = 1 / (1 + 10 ** ((ea - (eh + 60)) / 400))
        res_h = 1.0 if g["result"] == "H" else (0.5 if g["result"] == "D" else 0.0)
        elo[g["home_team_id"]] += k * (res_h - exp_h)
        elo[g["away_team_id"]] += k * ((1 - res_h) - (1 - exp_h))
        for t in (g["home_team_id"], g["away_team_id"]):
            if (g["date"], t) in seen:
                continue
            seen.add((g["date"], t))
            rows.append({"competition": "demo", "team_id": t, "as_of": g["date"],
                         "elo": round(elo[t], 1), "rank": None,
                         "last_game": None})
    return pd.DataFrame(rows)


def build_demo_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = duckdb.connect(str(path))
    conn.execute(open("schema/duckdb_schema.sql").read())
    games = pd.concat([_gen_season(START_YEAR + s) for s in range(N_SEASONS)],
                      ignore_index=True)
    # next round fixtures (scheduled, no scores) for the predict command
    fixtures = _gen_fixtures(START_YEAR + N_SEASONS)
    games = pd.concat([games, fixtures], ignore_index=True)
    teams = pd.DataFrame([{"competition": "demo", "team_id": t, "abbrev": t,
                           "name": f"Demo {t}"} for t in TEAMS])
    finals = games[games["status"] == "final"].reset_index(drop=True)
    tms = _team_stats(finals)
    od = _odds(finals)
    el = _elo(finals)
    conn.register("g_df", games)
    conn.register("teams_df", teams)
    conn.register("tms_df", tms)
    conn.register("od_df", od)
    conn.register("el_df", el)
    conn.execute(
        "INSERT OR REPLACE INTO games (competition, game_id, season, stage,"
        " date, starts_at, status, result, home_team_id, away_team_id,"
        " home_score, away_score, venue, city)"
        " SELECT competition, game_id, season, stage, date, starts_at, status,"
        " result, home_team_id, away_team_id, home_score, away_score, venue, city"
        " FROM g_df")
    conn.execute(
        "INSERT OR REPLACE INTO teams (competition, team_id, abbrev, name)"
        " SELECT competition, team_id, abbrev, name FROM teams_df")
    conn.execute(
        "INSERT OR REPLACE INTO team_match_stats SELECT * FROM tms_df")
    conn.execute(
        "INSERT OR REPLACE INTO odds_books (competition, game_id, provider,"
        " source, home_ml_open_orig, home_ml_open_dec, home_ml_close_orig,"
        " home_ml_close_dec, draw_ml_open_orig, draw_ml_open_dec,"
        " draw_ml_close_orig, draw_ml_close_dec, away_ml_open_orig,"
        " away_ml_open_dec, away_ml_close_orig, away_ml_close_dec,"
        " home_handicap_open, home_handicap_close, handicap_home_odds_orig,"
        " handicap_home_odds_dec, handicap_away_odds_orig, handicap_away_odds_dec,"
        " total_open, total_close, over_odds_orig, over_odds_dec,"
        " under_odds_orig, under_odds_dec, close_prob_raw, captured_at)"
        " SELECT competition, game_id, provider, source, home_ml_open_orig,"
        " home_ml_open_dec, home_ml_close_orig, home_ml_close_dec,"
        " draw_ml_open_orig, draw_ml_open_dec, draw_ml_close_orig,"
        " draw_ml_close_dec, away_ml_open_orig, away_ml_open_dec,"
        " away_ml_close_orig, away_ml_close_dec, home_handicap_open,"
        " home_handicap_close, handicap_home_odds_orig, handicap_home_odds_dec,"
        " handicap_away_odds_orig, handicap_away_odds_dec, total_open,"
        " total_close, over_odds_orig, over_odds_dec, under_odds_orig,"
        " under_odds_dec, close_prob_raw, captured_at FROM od_df")
    conn.execute(
        "INSERT OR REPLACE INTO elo_snapshots (competition, as_of, rank,"
        " team_id, elo, last_game)"
        " SELECT competition, as_of, rank, team_id, elo, last_game FROM el_df")
    conn.close()
    print(f"Demo DB generada: {path}  ({len(games)} partidos, "
          f"{len(tms)} team-stats, {len(od)} odds, {len(el)} elo)")