import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture()
def settings(tmp_path):
    from ingestion.config import Settings
    return Settings(
        api_key="test-key",
        base_url="https://example.test",
        db_path=tmp_path / "striker.db",
        raw_dir=tmp_path / "raw",
        monthly_budget=10000,
        reserve_credits=500,
        rate_per_minute=100000,   # no delay in tests
        low_balance_rate_per_minute=100000,
        timeout_seconds=5,
        max_retries=2,
        retry_backoff_base=0.0,
    )


@pytest.fixture()
def db(settings):
    from ingestion.db import Database
    d = Database(settings)
    d.migrate()
    yield d
    d.close()


@pytest.fixture()
def sample_get_game():
    return {
        "game": {
            "game_id": "740916", "season": 2025, "stage": "Regular Season",
            "date": "2026-04-19", "starts_at": "2026-04-19T15:30:00.000Z",
            "status": "final", "status_detail": "FT",
            "home": {"team_id": "382", "abbrev": "MNC", "name": "Manchester City", "score": 2},
            "away": {"team_id": "359", "abbrev": "ARS", "name": "Arsenal", "score": 1},
            "result": "H", "neutral_site": None, "venue": "Etihad", "city": "Manchester",
            "attendance": 52523,
        },
        "team_stats": [
            {"team_id": "382", "home": True, "goals": 2, "xg": 1.81, "possession_pct": 58.6,
             "shots": 15, "shots_on_target": 5, "blocked_shots": 5, "corners": 8,
             "fouls": 5, "yellow_cards": 2, "red_cards": 0, "offsides": 0, "saves": 1,
             "passes": 498, "accurate_passes": 424, "tackles": 12},
            {"team_id": "359", "home": False, "goals": 1, "xg": 1.37, "possession_pct": 41.4,
             "shots": 9, "shots_on_target": 3, "blocked_shots": 3, "corners": 5,
             "fouls": 12, "yellow_cards": 2, "red_cards": 0, "offsides": 4, "saves": 2,
             "passes": 348, "accurate_passes": 262},
        ],
        "players": [
            {"player_id": "196176", "name": "David Raya", "team_id": "359", "starter": True,
             "position": "Goalkeeper", "goals": 0, "saves": 2},
        ],
        "odds": [
            {"provider": "888sport", "source": "archive", "home_ml_open": 105,
             "home_ml_close": -118, "draw_ml_open": 250, "draw_ml_close": 250,
             "away_ml_open": 230, "away_ml_close": 300, "captured_at": "2026-04-19T15:20:38Z"},
            {"provider": "Bet 365", "source": "final", "home_ml_open": None,
             "home_ml_close": -118, "total_close": 2.5, "over_odds": -108, "under_odds": -108},
        ],
    }