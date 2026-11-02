"""End-to-end ingestor persistence test with mocked API (no network).

Verifica: se persisten partidos, stats, jugadores, cuotas; sin duplicados;
reanudacion no repite COMPLETED; los errores 402 abortan; el proceso vuelve
a coger PENDING.
"""
import pytest

from ingestion.config import Settings
from ingestion.credits import BudgetController
from ingestion.ingestor import Ingestor


class FakeSession:
    def __init__(self, payloads_by_id=None, fail_402_for=None):
        self.payloads_by_id = payloads_by_id or {}
        self.fail_402_for = set(fail_402_for or [])
        self.list_calls = []
        self.game_calls = []

    def post(self, url, json, headers, timeout):
        tool = url.rsplit("/", 1)[-1]
        if tool == "list_games":
            self.list_calls.append(json)
            return _r(200, {"games": [
                {"game_id": "1", "season": 2025, "starts_at": "2025-09-01T19:00:00Z",
                 "status": "final"},
                {"game_id": "2", "season": 2025, "starts_at": "2025-09-02T19:00:00Z",
                 "status": "final"},
                {"game_id": "3", "season": 2025, "starts_at": "2025-09-03T19:00:00Z",
                 "status": "final"},
            ], "next_offset": None})
        if tool == "get_game":
            gid = json["game_id"]
            self.game_calls.append(gid)
            if gid in self.fail_402_for:
                return _r(402, {"error": "Insufficient credits"})
            payload = self.payloads_by_id.get(gid)
            if payload is None:
                raise AssertionError(f"no payload for {gid}")
            return _r(200, payload)
        raise AssertionError(tool)


def _r(status, payload):
    import json as _json
    class R:
        def __init__(self):
            self.status_code = status
            self.ok = status == 200
            self.headers = {}
        def json(self):
            return payload
        @property
        def text(self):
            return _json.dumps(payload)
    return R()


def _payload(gid, status="final"):
    return {
        "game": {
            "game_id": gid, "season": 2025, "stage": "Regular Season",
            "date": "2025-09-01", "starts_at": "2025-09-01T19:00:00Z",
            "status": status, "status_detail": "FT",
            "home": {"team_id": "HH", "abbrev": "HOM", "name": "Home", "score": 2},
            "away": {"team_id": "AA", "abbrev": "AWA", "name": "Away", "score": 1},
            "result": "H", "venue": "V", "city": "C", "attendance": None,
        },
        "team_stats": [
            {"team_id": "HH", "home": True, "goals": 2, "xg": 1.0, "shots": 10},
            {"team_id": "AA", "home": False, "goals": 1, "xg": 0.6, "shots": 5},
        ],
        "players": [
            {"player_id": "P1", "name": "Jug1", "team_id": "HH", "starter": True,
             "goals": 1, "shots": 3},
        ],
        "odds": [
            {"provider": "B1", "source": "archive", "home_ml_open": 105,
             "home_ml_close": -118},
        ],
    }


def _fresh_settings(tmp_path):
    from ingestion.config import Settings
    return Settings(
        api_key="test", base_url="https://example.test",
        db_path=tmp_path / "s.db", raw_dir=tmp_path / "raw",
        monthly_budget=10000, reserve_credits=500,
        rate_per_minute=100000, low_balance_rate_per_minute=100000,
        timeout_seconds=5, max_retries=2, retry_backoff_base=0.0)


def test_full_pipeline_with_mock(tmp_path):
    settings = _fresh_settings(tmp_path)

    from ingestion.db import Database
    from ingestion.client import StrikerClient

    def cnt(table, where=""):
        row = db.fetchone(f"SELECT count(*) FROM {table} {where}")
        return row[0] if row else 0

    db = Database(settings)
    db.migrate()
    client = StrikerClient(settings)
    fake = FakeSession(payloads_by_id={gid: _payload(gid) for gid in ("1", "2", "3")})
    client.use_session(fake)
    budget = BudgetController(settings, db)
    ing = Ingestor(settings, client, db, budget)

    games = ing.discover_season("laliga", 2025)
    assert len(games) == 3
    done = ing.process_games(games, "laliga")
    assert done == 3

    assert cnt("games", "WHERE competition='laliga'") == 3
    assert cnt("team_match_stats", "WHERE game_id='1'") == 2
    assert cnt("player_match_stats", "WHERE game_id='1'") == 1
    assert cnt("odds_books", "WHERE game_id='1'") == 1
    assert cnt("teams", "WHERE competition='laliga'") == 2
    # coste: 3 partidos × 5 = 15 en get_game (+ 1 list_games de 2 creditos)
    assert budget.spent_this_month() >= 15

    # reanudar: los COMPLETED no se vuelven a llamar
    before = len(fake.game_calls)
    done2 = ing.process_games(games, "laliga")
    assert done2 == 0
    assert len(fake.game_calls) == before
    assert cnt("games", "WHERE competition='laliga'") == 3
    db.close()


def test_402_aborts(tmp_path):
    settings = _fresh_settings(tmp_path)
    from ingestion.db import Database
    from ingestion.client import StrikerClient

    def cnt(table, where=""):
        row = db.fetchone(f"SELECT count(*) FROM {table} {where}")
        return row[0] if row else 0

    db = Database(settings)
    db.migrate()
    client = StrikerClient(settings)
    client.use_session(FakeSession(
        payloads_by_id={gid: _payload(gid) for gid in ("1", "2", "3")},
        fail_402_for=["2"]))
    budget = BudgetController(settings, db)
    ing = Ingestor(settings, client, db, budget)

    games = ing.discover_season("laliga", 2025)
    with pytest.raises(Exception):
        ing.process_games(games, "laliga")
    # ingest order: fecha desc -> g3 primero, luego g2 (402 aborta), g1 nunca
    assert cnt("team_match_stats", "WHERE game_id='3'") == 2
    assert cnt("team_match_stats", "WHERE game_id='1'") == 0
    db.close()