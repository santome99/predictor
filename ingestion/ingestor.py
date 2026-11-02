"""Orchestration: discover games (list_games), enrich (get_game), track credits.

Resumable, idempotent, budget-aware. Order: seasons desc, games desc by starts_at.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from ingestion.client import (CreditsExhaustedError, InvalidRequestError,
                              StrikerClient, StrikerAPIError)
from ingestion.config import Settings
from ingestion.credits import BudgetController
from ingestion.db import Database
from ingestion.normalize import (elo_rows, game_row, odds_rows,
                                 player_stats_rows, team_rows, team_season_rows,
                                 team_stats_rows)


log = logging.getLogger("strikerapi.ingestor")


class Ingestor:
    def __init__(self, settings: Settings, client: StrikerClient,
                 db: Database, budget: BudgetController):
        self.s = settings
        self.client = client
        self.db = db
        self.budget = budget
        self.calls = 0
        client.set_on_spend(lambda tool, credits: budget.spend(tool, credits))
        client.set_on_raw(lambda tool, key, path, size:
                          db.record_raw(self.s.competition, tool, key, path,
                                        self.s.cost_of(tool), size))

    # ------------------------------------------------------------ discovery
    def discover_season(self, competition: str, season: int, limit: int = 50,
                        force: bool = False) -> list:
        """Paginado con list_games. Deja los partidos y equios en la BD y
        crea checkpoints PENDING para los finales."""
        games_all = []
        offset = 0
        while True:
            params = {"season": season, "status": "final", "limit": limit}
            if offset:
                params["offset"] = offset
            payload = self.client.call("list_games", params)
            games = payload.get("games") or []
            games_all.extend(games)

            nxt = payload.get("next_offset")
            self.calls += 1
            if not games or nxt is None or not isinstance(nxt, int) or nxt <= offset:
                break
            offset = nxt

        # dar de alta partidos y equipos (idempotente)
        for game in games_all:
            comp = competition
            self.db.upsert_rows("games", [game_row(game, comp)])
            self.db.upsert_rows("teams", team_rows(game, comp))
            self.db.upsert_rows("team_seasons",
                                team_season_rows(game, comp, game.get("season")))

        valid = [g for g in games_all if g.get("status") == "final"]
        for g in valid:
            if force:
                self._mark(g, "PENDING")
            else:
                self._ensure_pending(g, competition)
        return valid

    # ------------------------------------------------------------ processing
    def process_games(self, games: list, competition: str, mode: str = "run",
                      max_games: Optional[int] = 0) -> int:
        """Descarga get_game de cada partido y persiste todo. Devuelve nº OK."""
        job_id = self.db.new_job(
            competition, (games[0] or {}).get("season") if games else None, mode)
        done = skipped = failed = 0
        ordered = sorted(games, key=lambda g: g.get("starts_at") or "", reverse=True)
        for g in ordered:
            if max_games and done >= max_games:
                break
            gid = g.get("game_id")
            if not gid:
                continue
            if self._should_skip(g, competition):
                skipped += 1
                continue
            try:
                self._set_processing(gid, competition)
                self._process_one(gid, competition)
                self._mark_completed(gid, competition)
                done += 1
            except CreditsExhaustedError as e:
                self._mark_failed(gid, competition, str(e))
                log.error("402 -> detengo ingesta: %s", e)
                self.db.finish_job(job_id, status="aborted",
                                   games_done=done, games_failed=failed,
                                   games_skipped=skipped,
                                   credits_used=self.budget.spent_this_month())
                raise
            except InvalidRequestError as e:
                self._mark_failed(gid, competition, str(e))
                log.error("error permanente en %s: %s", gid, e)
                failed += 1
            except (StrikerAPIError, Exception) as e:   # noqa: BLE001
                self._mark_failed(gid, competition, str(e))
                log.error("error recuperable en %s: %s", gid, e)
                failed += 1

        self.db.finish_job(job_id, status="completed", games_done=done,
                           games_failed=failed, games_skipped=skipped,
                           credits_used=self.budget.spent_this_month())
        return done

    def _process_one(self, game_id: str, competition: str) -> None:
        if not self.budget.preflight("get_game", self.s.cost_of("get_game")):
            raise CreditsExhaustedError("presupuesto agotado antes de get_game", tool="get_game")
        payload = self.client.call(
            "get_game", {"game_id": game_id},
            raw_dir=self.s.raw_dir, raw_key=f"game:{game_id}")
        game = payload.get("game") or {}
        if not game:
            log.warning("get_game sin game para %s (respuesta vacía/refunded)", game_id)
            return

        season = game.get("season")
        self.db.upsert_rows("games", [game_row(game, competition)])
        self.db.upsert_rows("teams", team_rows(game, competition))
        self.db.upsert_rows("team_seasons", team_season_rows(game, competition, season))

        by_table = {
            "team_match_stats": [
                {**r, "game_id": r["game_id"] or game_id}
                for r in team_stats_rows(payload.get("team_stats") or [], competition)
            ],
            "player_match_stats": [
                {**r, "game_id": r["game_id"] or game_id}
                for r in player_stats_rows(payload.get("players") or [], competition)
            ],
            "odds_books": [
                {**r, "game_id": r["game_id"] or game_id}
                for r in odds_rows(payload.get("odds") or [], competition)
            ],
        }
        for table, items in by_table.items():
            self.db.upsert_rows(table, items)

    # ------------------------------------------------------------- checkpoint
    def _ensure_pending(self, game: dict, competition: str, attempts=0) -> None:
        row = self.db.fetchone(
            "SELECT ingestion_status FROM ingestion_checkpoints "
            "WHERE competition=? AND game_id=?", [competition, game.get("game_id")])
        if row is None:
            self._mark(game, "PENDING")

    def _should_skip(self, game: dict, competition: str) -> bool:
        gid = game.get("game_id")
        row = self.db.fetchone(
            "SELECT ingestion_status, attempts FROM ingestion_checkpoints "
            "WHERE competition=? AND game_id=?", [competition, gid])
        if row is None:
            return False
        status, attempts = row
        if status == "COMPLETED":
            return True
        if status == "FAILED" and attempts >= self.s.max_retries:
            log.warning("%s excede reintentos (%s) — se omite salvo 'retry'", gid, attempts)
            return True
        return False

    def _mark(self, game, status: str) -> None:
        gid = game.get("game_id")
        self.db.upsert_checkpoint({
            "competition": game.get("competition", self.s.competition),
            "season": game.get("season") or None,
            "game_id": gid,
            "starts_at": game.get("starts_at"),
            "ingestion_status": status,
            "attempts": 0, "last_attempt": None, "completed_at": None,
            "error_message": None,
        })

    def _set_processing(self, gid: str, competition: str) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.db.execute(
            "UPDATE ingestion_checkpoints SET ingestion_status='PROCESSING', "
            "last_attempt=? WHERE competition=? AND game_id=? AND "
            "ingestion_status NOT IN ('COMPLETED')",
            [now, competition, gid])

    def _mark_completed(self, gid: str, competition: str) -> None:
        self.db.execute(
            "UPDATE ingestion_checkpoints SET ingestion_status='COMPLETED', "
            "completed_at=now(), attempts=attempts+1, last_attempt=now(), "
            "error_message=NULL WHERE competition=? AND game_id=?",
            [competition, gid])

    def _mark_failed(self, gid: str, competition: str, err: str) -> None:
        self.db.execute(
            "UPDATE ingestion_checkpoints SET ingestion_status='FAILED', "
            "attempts=attempts+1, last_attempt=now(), error_message=? "
            "WHERE competition=? AND game_id=?",
            [err[:500], competition, gid])

    # -------------------------------------------------------------- Elo (get_ratings)
    def ingest_elo_for_season(self, competition: str, season: int,
                              cached: bool = True) -> int:
        """Ratings por cada fecha distinta de partidos de la temporada.

        Uso anti-fuga: solo registramos el snapshot ``as_of``; el join de
        features filtra ``as_of < fecha del partido objetivo``. Una llamada por
        fecha única (barato: 2 cr). Saliendo de rango con 402 -> aborta.
        """
        dates = self.db.fetchall(
            "SELECT DISTINCT date FROM games WHERE competition=? AND season=? AND date IS NOT NULL ORDER BY date",
            [competition, season])
        done = 0
        for (d,) in dates:
            if cached and self.db.fetchone(
                    "SELECT 1 FROM elo_snapshots WHERE competition=? AND as_of=DATE(?)",
                    [competition, d]):
                continue
            if not self.budget.preflight("get_ratings", self.s.cost_of("get_ratings")):
                raise CreditsExhaustedError("presupuesto agotado antes de get_ratings",
                                            tool="get_ratings")
            payload = self.client.call(
                "get_ratings", {"date": d.isoformat() if not isinstance(d, str) else d},
                raw_dir=self.s.raw_dir, raw_key=f"ratings:{d}")
            rows = elo_rows(payload, competition)
            if rows:
                self.db.upsert_rows("elo_snapshots", rows)
                done += len(rows)
        return done


# ============================================================== helpers CLI
def list_final_games(db: Database, competition: str, season: Optional[int]) -> int:
    """Nº de partidos finales ya en BD para una temporada (para preflight)."""
    if season is None:
        row = db.fetchone(
            "SELECT MAX(season) FROM games WHERE competition=? AND status='final'",
            [competition])
        return _scalar(row)
    row = db.fetchone(
        "SELECT count(*) FROM games WHERE competition=? AND season=? AND status='final'",
        [competition, season])
    return _scalar(row)


def _scalar(queue_row) -> int:
    return queue_row[0] if queue_row else 0