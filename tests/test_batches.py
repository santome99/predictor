"""Simula el flujo real por TANDAS con claves API distintas.

Objetivo: demostrar que (1) el gasto se contabiliza por credencial, (2) la
segunda tanda no recompra los partidos COMPLETED, y (3) el estado queda
consistente si una tanda se corta.
"""
import pytest

from ingestion.config import Settings
from ingestion.credits import BudgetController
from ingestion.db import Database
from ingestion.ingestor import Ingestor


class FakeClient:
    """Devuelve un get_game mínimo válido para cualquier game_id."""

    def __init__(self):
        self.calls = []
        self.on_spend = None
        self.on_raw = None

    def set_on_spend(self, cb):
        self.on_spend = cb

    def set_on_raw(self, cb):
        self.on_raw = cb

    def call(self, tool, params, raw_dir=None, raw_key=None):
        self.calls.append((tool, params.get("game_id")))
        if tool != "get_game":
            raise AssertionError(tool)
        gid = params["game_id"]
        # fiel al cliente real: al exito se cobra el coste documentado
        if self.on_spend is not None:
            self.on_spend(tool, 5)
        return {
            "game": {
                "game_id": gid, "season": 2025, "starts_at": "2025-09-01T19:00:00Z",
                "status": "final", "result": "H",
                "home": {"team_id": "H", "name": "Home", "score": 2},
                "away": {"team_id": "A", "name": "Away", "score": 1},
            },
            "team_stats": [
                {"team_id": "H", "home": True, "goals": 2},
                {"team_id": "A", "home": False, "goals": 1},
            ],
            "players": [],
            "odds": [],
        }


def _settings(db_path, raw_dir, label, key):
    return Settings(api_key=key, base_url="https://x", db_path=db_path,
                    raw_dir=raw_dir, monthly_budget=10000, reserve_credits=50,
                    credits_remaining=500, credential_label=label,
                    rate_per_minute=100000, low_balance_rate_per_minute=100000,
                    max_retries=1, retry_backoff_base=0.0)


def _games(n):
    return [{"game_id": f"g{i}", "season": 2025,
             "starts_at": f"2025-09-{i+1:02d}T19:00:00Z", "competition": "laliga"}
            for i in range(n)]


def test_two_batches_different_keys(tmp_path):
    db_path = tmp_path / "s.db"
    raw = tmp_path / "raw"

    # --- tanda 1: clave A, 3 partidos ---
    sA = _settings(db_path, raw, "tanda1", "keyA")
    db = Database(sA)
    db.migrate()
    bA = BudgetController(sA, db)
    cA = FakeClient()
    ingA = Ingestor(sA, cA, db, bA)
    games = _games(5)
    doneA = ingA.process_games(games, "laliga", mode="run", max_games=3)
    assert doneA == 3
    # orden descendente por fecha: primero los mas recientes g4, g3, g2
    assert sorted(gid for _, gid in cA.calls) == ["g2", "g3", "g4"]
    assert bA.spent_this_month() == 15          # 3 x get_game (5 cr)
    assert bA.available() == 435                # 450 - 15

    # --- tanda 2: clave B, resto ---
    sB = _settings(db_path, raw, "tanda2", "keyB")
    bB = BudgetController(sB, db)
    assert bB.credential == "tanda2"
    assert bB.spent_this_month() == 0            # contador propio de la clave B
    assert bB.available() == 450
    cB = FakeClient()
    ingB = Ingestor(sB, cB, db, bB)
    doneB = ingB.process_games(games, "laliga", mode="resume")
    assert doneB == 2                            # solo los 2 pendientes
    # no se recompran los 3 ya completados
    assert sorted(gid for _, gid in cB.calls) == ["g0", "g1"]
    assert bB.spent_this_month() == 10

    # estado global
    st = {r[2]: r[4] for r in db.checkpoints()}
    assert st == {f"g{i}": "COMPLETED" for i in range(5)}
    assert db.fetchone("SELECT count(*) FROM games")[0] == 5
    # gasto total por credencial, visible por separado
    assert db.month_spent(bA.month, "tanda1") == 15
    assert db.month_spent(bA.month, "tanda2") == 10
    assert db.month_spent(bA.month) == 25        # total del mes
    db.close()


def test_budget_stops_batch_and_leaves_pending(tmp_path):
    """Si la tanda se queda sin presupuesto, el resto queda PENDING (no FAILED)."""
    from ingestion.client import CreditsExhaustedError
    db_path = tmp_path / "s.db"
    raw = tmp_path / "raw"

    class DryClient(FakeClient):
        def call(self, tool, params, raw_dir=None, raw_key=None):
            raise CreditsExhaustedError("Insufficient credits", 402, tool)

    # cap = min(10000, 60) - 50 = 10 -> solo caben 2 llamadas de 5 cr
    s = Settings(api_key="k", base_url="https://x", db_path=db_path, raw_dir=raw,
                 monthly_budget=10000, reserve_credits=50, credits_remaining=60,
                 credential_label="tandaX", rate_per_minute=100000,
                 low_balance_rate_per_minute=100000, max_retries=1,
                 retry_backoff_base=0.0)
    db = Database(s)
    db.migrate()
    b = BudgetController(s, db)
    ing = Ingestor(s, DryClient(), db, b)
    with pytest.raises(CreditsExhaustedError):
        ing.process_games(_games(4), "laliga", mode="resume")
    st = {r[2]: (r[4], r[5]) for r in db.checkpoints()}
    # todos existen y ninguno se marca FAILED por falta de presupuesto
    assert len(st) == 4
    assert all(v[0] == "PENDING" and v[1] == 0 for v in st.values())
    db.close()
