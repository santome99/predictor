"""Resume semantics: an interrupted run does not recompute COMPLETED games."""
from ingestion.ingestor import Ingestor


class FakeClient:
    def __init__(self):
        self.called = []
        self.raw_keys = []
        self.on_spend = None
        self.on_raw = None

    def set_on_spend(self, cb):
        self.on_spend = cb

    def set_on_raw(self, cb):
        self.on_raw = cb

    def call(self, tool, params, raw_dir=None, raw_key=None):  # noqa: A002
        self.called.append((tool, params))
        if raw_key:
            self.raw_keys.append(raw_key)
        if tool == "get_game":
            return {"game": {
                "game_id": params["game_id"], "season": 2025,
                "starts_at": "2025-09-01T19:00:00Z", "status": "final", "result": "H",
                "home": {"team_id": "H", "name": "Home"},
                "away": {"team_id": "A", "name": "Away"},
            }, "team_stats": [], "players": [], "odds": []}


def _mk_ingestor(db, fake):
    from ingestion.credits import BudgetController
    from ingestion.config import Settings
    s = Settings(api_key="x", base_url="https://x", db_path=db.s.db_path,
                 raw_dir=db.s.raw_dir)
    budget = BudgetController.__new__(BudgetController)
    budget.s = s
    budget.db = db
    budget.month = "2026-10"
    budget.credential = "test"
    return Ingestor(s, fake, db, budget)


def test_resume_skips_completed(db):
    # g1 COMPLETED, g2 PENDING (interrumpido), g3 PENDING
    db.upsert_checkpoint({
        "competition": "laliga", "season": 2025, "game_id": "g1",
        "starts_at": "2025-09-03", "ingestion_status": "COMPLETED",
        "attempts": 1, "last_attempt": None, "completed_at": None, "error_message": None,
    })
    for gid in ("g2", "g3"):
        db.upsert_checkpoint({
            "competition": "laliga", "season": 2025, "game_id": gid,
            "starts_at": "2025-09-01", "ingestion_status": "PENDING",
            "attempts": 0, "last_attempt": None, "completed_at": None, "error_message": None,
        })

    fake = FakeClient()
    # games que devuelve el resume (solo pendientes)
    games = [
        {"game_id": "g2", "season": 2025, "starts_at": "2025-09-01", "competition": "laliga"},
        {"game_id": "g3", "season": 2025, "starts_at": "2025-09-01", "competition": "laliga"},
    ]
    ing = _mk_ingestor(db, fake)
    done = ing.process_games(games, "laliga", mode="resume")
    assert done == 2
    assert len(fake.called) == 2                      # solo g2,g3
    assert not any(p for (t, p) in fake.called if p["game_id"] == "g1")
    st = {r[2]: r[4] for r in db.checkpoints()}
    assert st["g1"] == "COMPLETED"
    assert st["g2"] == "COMPLETED"
    assert st["g3"] == "COMPLETED"


def test_failed_with_max_attempts_skipped(db):
    for gid, attempts in (("gA", 0), ("gB", 99)):
        db.upsert_checkpoint({
            "competition": "laliga", "season": 2025, "game_id": gid,
            "starts_at": "2025-09-01", "ingestion_status": "FAILED",
            "attempts": attempts, "last_attempt": None, "completed_at": None,
            "error_message": "boom",
        })
    fake = FakeClient()
    games = [{"game_id": "gA", "starts_at": "2025-09-01"},
             {"game_id": "gB", "starts_at": "2025-09-01"}]
    ing = _mk_ingestor(db, fake)
    done = ing.process_games(games, "laliga", mode="resume")
    # gA (attempts 0 < max) reintentado; gB (attempts 99 > max) omitido
    assert done == 1
    assert [p["game_id"] for (t, p) in fake.called] == ["gA"]

class FailingClient(FakeClient):
    def __init__(self, fail_402_for=(), fail_400_for=()):
        super().__init__()
        self.fail_402 = set(fail_402_for)
        self.fail_400 = set(fail_400_for)

    def call(self, tool, params, raw_dir=None, raw_key=None):
        from ingestion.client import CreditsExhaustedError, InvalidRequestError
        gid = params.get("game_id")
        if gid in self.fail_402:
            raise CreditsExhaustedError("Insufficient credits", 402, tool)
        if gid in self.fail_400:
            raise InvalidRequestError("bad request", 400, tool)
        return super().call(tool, params, raw_dir, raw_key)


def test_checkpoint_created_if_missing_and_402_keeps_pending(db):
    """Un corte por presupuesto NO consume intento ni marca FAILED."""
    from ingestion.credits import BudgetController
    from ingestion.config import Settings
    import pytest as _pytest

    s = Settings(api_key="x", base_url="https://x", db_path=db.s.db_path,
                 raw_dir=db.s.raw_dir, max_retries=2)
    budget = BudgetController(s, db)
    fake = FailingClient(fail_402_for=["g1"])
    ing = Ingestor(s, fake, db, budget)

    # BD sin checkpoints: process_games debe crearlos y luego detenerse en g1
    games = [{"game_id": "g1", "season": 2025, "starts_at": "2025-09-02"},
             {"game_id": "g2", "season": 2025, "starts_at": "2025-09-01"}]
    with _pytest.raises(Exception):
        ing.process_games(games, "laliga", mode="resume")

    by = {r[2]: (r[4], r[5]) for r in db.checkpoints()}
    # g1 (mas reciente, falla por 402) queda PENDING con 0 intentos
    assert by["g1"] == ("PENDING", 0)
    # g2 no se ha procesado pero existe su checkpoint
    assert by["g2"][0] == "PENDING"


def test_400_marks_failed_with_attempt():
    from ingestion.credits import BudgetController
    from ingestion.config import Settings
    from ingestion.db import Database
    import tempfile, pathlib

    tmp = pathlib.Path(tempfile.mkdtemp())
    s = Settings(api_key="x", base_url="https://x", db_path=tmp / "t.db",
                 raw_dir=tmp / "raw", max_retries=2)
    db = Database(s)
    db.migrate()
    budget = BudgetController(s, db)
    fake = FailingClient(fail_400_for=["g9"])
    ing = Ingestor(s, fake, db, budget)
    ing.process_games([{"game_id": "g9", "season": 2025,
                        "starts_at": "2025-09-01"}], "laliga", mode="resume")
    row = db.fetchone(
        "SELECT ingestion_status, attempts FROM ingestion_checkpoints WHERE game_id='g9'")
    assert row[0] == "FAILED" and row[1] == 1
    db.close()
