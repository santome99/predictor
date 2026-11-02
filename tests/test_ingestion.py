"""Tests: normalization, DB persistence, idempotency, pagination, checkpoint resume."""
import pytest

from ingestion.normalize import (game_row, odds_rows, player_stats_rows,
                                 team_rows, team_season_rows, team_stats_rows)


def test_game_row(sample_get_game):
    g = sample_get_game["game"]
    row = game_row(g, "laliga")
    assert row["game_id"] == "740916"
    assert row["home_team_id"] == "382"
    assert row["away_team_id"] == "359"
    assert row["home_score"] == 2 and row["away_score"] == 1
    assert row["result"] == "H"


def test_team_rows_dedup(sample_get_game):
    rows = team_rows(sample_get_game["game"], "laliga")
    assert len(rows) == 2
    ts = team_season_rows(sample_get_game["game"], "laliga", 2025)
    assert len(ts) == 2
    assert all(r["season"] == 2025 for r in ts)


def test_stats_rows(sample_get_game):
    ts = team_stats_rows(sample_get_game["team_stats"], "laliga", game_id="740916")
    assert len(ts) == 2
    assert ts[0]["xg"] == 1.81
    # absent fields must stay None (not 0): "long_balls" not present
    assert ts[0]["long_balls"] is None
    assert ts[0]["accurate_long_balls"] is None


def test_player_rows(sample_get_game):
    pr = player_stats_rows(sample_get_game["players"], "laliga")
    assert len(pr) == 1
    assert pr[0]["starter"] is True
    # game_id is filled later by ingestor; null here is fine
    assert pr[0]["game_id"] is None


def test_odds_rows_original_preserved(sample_get_game):
    odds = odds_rows(sample_get_game["odds"], "laliga")
    assert len(odds) == 2
    arch = next(o for o in odds if o["provider"] == "888sport")
    assert arch["home_ml_open_orig"] == 105
    assert arch["home_ml_open_dec"] == 2.05
    assert arch["home_ml_close_orig"] == -118
    assert abs(arch["home_ml_close_dec"] - 1.8475) < 1e-3
    final = next(o for o in odds if o["provider"] == "Bet 365")
    assert final["source"] == "final"
    assert final["home_ml_open_orig"] is None and final["home_ml_open_dec"] is None
    assert final["total_close"] == 2.5
    assert final["over_odds_dec"] == pytest.approx(1 + 100 / 108, rel=1e-3)


def test_persistence_idempotent(db, sample_get_game):
    from ingestion.db import Database
    g = sample_get_game

    games_rows = [game_row(g["game"], "laliga")]
    teams_rows = team_rows(g["game"], "laliga")
    ts_rows = team_stats_rows(g["team_stats"], "laliga", game_id="740916")
    pl_rows = player_stats_rows(g["players"], "laliga", game_id="740916")
    od_rows = odds_rows(g["odds"], "laliga", game_id="740916")

    # primera carga
    db.upsert_rows("games", games_rows)
    db.upsert_rows("teams", teams_rows)
    db.upsert_rows("team_match_stats", ts_rows)
    db.upsert_rows("player_match_stats", pl_rows)
    db.upsert_rows("odds_books", od_rows)

    # re-carga idempotente (misma PK)
    db.upsert_rows("games", games_rows)
    db.upsert_rows("team_match_stats", ts_rows)
    db.upsert_rows("odds_books", od_rows)

    assert db.fetchone("SELECT count(*) FROM games")[0] == 1
    assert db.fetchone("SELECT count(*) FROM teams")[0] == 2
    assert db.fetchone("SELECT count(*) FROM team_match_stats")[0] == 2
    assert db.fetchone("SELECT count(*) FROM odds_books")[0] == 2


def test_null_never_zero(db, sample_get_game):
    ts_rows = team_stats_rows(sample_get_game["team_stats"], "laliga", game_id="740916")
    db.upsert_rows("team_match_stats", ts_rows)
    row = db.fetchone("SELECT xg, long_balls FROM team_match_stats WHERE team_id='382'")
    assert row[1] is None  # long_balls no present -> NULL, not 0


def test_pagination():
    """Simula la paginacion por offset/next_offset de list_games."""
    from ingestion import ingestor
    calls = []

    class FakeClient:
        def call(self, tool, params):
            if tool != "list_games":
                raise AssertionError(tool)
            off = params.get("offset", 0)
            batch = list(range(off, min(off + 50, 125)))
            calls.append(params)
            return {
                "games": [{"game_id": f"g{i}", "season": 2025, "starts_at": f"2025-01-{i%28+1:02d}",
                           "status": "final"}
                          for i in batch],
                "next_offset": (off + 50) if (off + 50) < 125 else None,
            }

    class FakeDB:
        def upsert_rows(self, table, rows):
            pass
        def fetchone(self, sql, params):
            return None
        def upsert_checkpoint(self, row):
            pass

    class TIngestor(ingestor.Ingestor):
        """sobreescribe __init__ para no tocar la API en el test."""
        def __init__(self):
            self.client = FakeClient()
            self.db = FakeDB()
            self.s = type("S", (), {"competition": "laliga", "max_retries": 2,
                                    "cost_of": lambda self, t, default=5: 5})()
            self.calls = 0

    ing = TIngestor()
    games = ing.discover_season("laliga", 2025)
    assert len(games) == 125
    assert len(calls) == 3
    assert calls[1]["offset"] == 50
    assert calls[2]["offset"] == 100


def test_checkpoint_resume(db):
    # estado inicial: ninguno COMPLETED
    db.upsert_checkpoint({
        "competition": "laliga", "season": 2025, "game_id": "g1",
        "starts_at": "2025-01-01T19:00:00Z", "ingestion_status": "COMPLETED",
        "attempts": 1, "last_attempt": None, "completed_at": None, "error_message": None,
    })
    db.upsert_checkpoint({
        "competition": "laliga", "season": 2025, "game_id": "g2",
        "starts_at": "2025-01-02T19:00:00Z", "ingestion_status": "FAILED",
        "attempts": 1, "last_attempt": None, "completed_at": None, "error_message": "boom",
    })
    cps = db.checkpoints()
    by = {c[2]: c[4] for c in cps}
    assert by == {"g1": "COMPLETED", "g2": "FAILED"}  # nada PENDING => reanudar no repite