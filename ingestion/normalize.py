"""Mappers from StrikerAPI JSON payloads to normalised DB rows.

Every optional/numeric field is kept as-is (NULL stays NULL, never turned to 0).
Original odds values are preserved; decimal equivalents are derived separately.
"""
from __future__ import annotations

import json
from typing import Optional

from ingestion.prices import american_to_decimal

_game_fields = [
    "game_id", "season", "stage", "date", "starts_at", "status",
    "status_detail", "home_team_id", "away_team_id", "home_score",
    "away_score", "result", "neutral_site", "venue", "city", "attendance",
]


def _g(game: dict, competition: str) -> dict:
    home = game.get("home") or {}
    away = game.get("away") or {}
    return {
        "competition": competition,
        "game_id": game.get("game_id"),
        "season": game.get("season") or None,
        "stage": game.get("stage"),
        "date": game.get("date"),
        "starts_at": game.get("starts_at"),
        "status": game.get("status"),
        "status_detail": game.get("status_detail"),
        "home_team_id": home.get("team_id"),
        "away_team_id": away.get("team_id"),
        "home_score": home.get("score") if isinstance(home.get("score"), int) else None,
        "away_score": away.get("score") if isinstance(away.get("score"), int) else None,
        "result": game.get("result"),
        "neutral_site": game.get("neutral_site"),
        "venue": game.get("venue"),
        "city": game.get("city"),
        "attendance": game.get("attendance"),
    }


def game_row(game: dict, competition: str) -> dict:
    return _g(game, competition)


def _team_sides(game: dict):
    for side in ("home", "away"):
        t = game.get(side) or {}
        if t.get("team_id"):
            yield t


def team_rows(game: dict, competition: str) -> list:
    """Dimension teams from a game payload (deduped)."""
    rows = {}
    for t in _team_sides(game):
        tid = t["team_id"]
        rows.setdefault(tid, {
            "competition": competition, "team_id": tid,
            "abbrev": t.get("abbrev"), "name": t.get("name"),
            "display_name": t.get("name"), "short_name": t.get("name"),
            "location": None, "color": None, "logo": None,
        })
    return list(rows.values())


def team_season_rows(game: dict, competition: str, season=None) -> list:
    """Team membership for a season (deduped)."""
    season = season or game.get("season")
    rows = {}
    for t in _team_sides(game):
        tid = t["team_id"]
        rows.setdefault((tid, season), {
            "competition": competition, "season": season, "team_id": tid,
            "abbrev": t.get("abbrev"), "name": t.get("name"),
        })
    return list(rows.values())


def _ints(v):
    return v if isinstance(v, int) else None


def team_stats_rows(team_stats: list, competition: str,
                    game_id: Optional[str] = None) -> list:
    rows = []
    for ts in (team_stats or []):
        tidy = ts.get("team_id")
        if not tidy:
            continue
        rows.append({
            "competition": competition,
            "game_id": game_id or ts.get("game_id"),
            "team_id": tidy,
            "home": ts.get("home"),
            "goals": _ints(ts.get("goals")),
            "xg": ts.get("xg"),
            "possession_pct": ts.get("possession_pct"),
            "shots": _ints(ts.get("shots")),
            "shots_on_target": _ints(ts.get("shots_on_target")),
            "blocked_shots": _ints(ts.get("blocked_shots")),
            "corners": _ints(ts.get("corners")),
            "fouls": _ints(ts.get("fouls")),
            "yellow_cards": _ints(ts.get("yellow_cards")),
            "red_cards": _ints(ts.get("red_cards")),
            "offsides": _ints(ts.get("offsides")),
            "saves": _ints(ts.get("saves")),
            "passes": _ints(ts.get("passes")),
            "accurate_passes": _ints(ts.get("accurate_passes")),
            "crosses": _ints(ts.get("crosses")),
            "accurate_crosses": _ints(ts.get("accurate_crosses")),
            "long_balls": _ints(ts.get("long_balls")),
            "accurate_long_balls": _ints(ts.get("accurate_long_balls")),
            "tackles": _ints(ts.get("tackles")),
            "effective_tackles": _ints(ts.get("effective_tackles")),
            "interceptions": _ints(ts.get("interceptions")),
            "clearances": _ints(ts.get("clearances")),
            "penalty_goals": _ints(ts.get("penalty_goals")),
            "penalty_attempts": _ints(ts.get("penalty_attempts")),
        })
    return rows


def player_stats_rows(players: list, competition: str,
                      game_id: Optional[str] = None) -> list:
    rows = []
    for p in (players or []):
        pid = p.get("player_id")
        if not pid:
            continue
        rows.append({
            "competition": competition,
            "game_id": game_id or p.get("game_id"),
            "player_id": pid,
            "player_name": p.get("name"),
            "team_id": p.get("team_id"),
            "starter": p.get("starter"),
            "position": p.get("position"),
            "jersey": p.get("jersey"),
            "minute_on": _ints(p.get("minute_on")),
            "minute_off": _ints(p.get("minute_off")),
            "goals": _ints(p.get("goals")),
            "assists": _ints(p.get("assists")),
            "shots": _ints(p.get("shots")),
            "shots_on_target": _ints(p.get("shots_on_target")),
            "fouls_committed": _ints(p.get("fouls_committed")),
            "fouls_suffered": _ints(p.get("fouls_suffered")),
            "offsides": p.get("offsides"),
            "yellow_cards": _ints(p.get("yellow_cards")),
            "red_cards": _ints(p.get("red_cards")),
            "own_goals": _ints(p.get("own_goals")),
            "saves": _ints(p.get("saves")),
            "goals_conceded": _ints(p.get("goals_conceded")),
            "shots_faced": _ints(p.get("shots_faced")),
        })
    return rows


def odds_rows(odds: list, competition: str, game_id: Optional[str] = None) -> list:
    """One row per (provider, source). Original + decimal odds."""
    rows = []
    for b in (odds or []):
        provider = b.get("provider")
        if not provider:
            continue
        rows.append({
            "competition": competition,
            "game_id": game_id or b.get("game_id"),
            "provider": provider,
            "source": b.get("source"),
            "home_ml_open_orig": b.get("home_ml_open"),
            "home_ml_open_dec": american_to_decimal(b.get("home_ml_open")),
            "home_ml_close_orig": b.get("home_ml_close"),
            "home_ml_close_dec": american_to_decimal(b.get("home_ml_close")),
            "draw_ml_open_orig": b.get("draw_ml_open"),
            "draw_ml_open_dec": american_to_decimal(b.get("draw_ml_open")),
            "draw_ml_close_orig": b.get("draw_ml_close"),
            "draw_ml_close_dec": american_to_decimal(b.get("draw_ml_close")),
            "away_ml_open_orig": b.get("away_ml_open"),
            "away_ml_open_dec": american_to_decimal(b.get("away_ml_open")),
            "away_ml_close_orig": b.get("away_ml_close"),
            "away_ml_close_dec": american_to_decimal(b.get("away_ml_close")),
            "home_handicap_open": b.get("home_handicap_open"),
            "home_handicap_close": b.get("home_handicap_close"),
            "handicap_home_odds_orig": b.get("handicap_home_odds"),
            "handicap_home_odds_dec": american_to_decimal(b.get("handicap_home_odds")),
            "handicap_away_odds_orig": b.get("handicap_away_odds"),
            "handicap_away_odds_dec": american_to_decimal(b.get("handicap_away_odds")),
            "total_open": b.get("total_open"),
            "total_close": b.get("total_close"),
            "over_odds_orig": b.get("over_odds"),
            "over_odds_dec": american_to_decimal(b.get("over_odds")),
            "under_odds_orig": b.get("under_odds"),
            "under_odds_dec": american_to_decimal(b.get("under_odds")),
            "close_prob_raw": _serialize(b.get("close_prob")),
            "captured_at": b.get("captured_at"),
        })
    return rows


def _serialize(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v)
    except Exception:
        return str(v)


def elo_rows(payload: dict, competition: str) -> list:
    """get_ratings payload -> one row per (as_of, team)."""
    as_of = payload.get("as_of")
    if not as_of:
        return []
    rows = []
    for r in (payload.get("ratings") or []):
        tid = r.get("team_id")
        if not tid:
            continue
        rows.append({
            "competition": competition,
            "as_of": as_of,
            "rank": r.get("rank"),
            "team_id": tid,
            "abbrev": r.get("abbrev"),
            "name": r.get("name"),
            "elo": r.get("elo"),
            "last_game": r.get("last_game"),
        })
    return rows


def game_id_of(payload: dict) -> Optional[str]:
    g = payload.get("game") or {}
    return g.get("game_id")