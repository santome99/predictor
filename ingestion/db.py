"""DuckDB persistence: schema, upserts, and raw-response ledger."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

import duckdb

from ingestion.config import Settings

log = logging.getLogger("strikerapi.db")

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schema" / "duckdb_schema.sql"


class Database:
    """Thin wrapper around a DuckDB file database with idempotent upserts."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.s.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.s.db_path))

    # ------------------------------------------------------------ lifecycle
    def migrate(self) -> None:
        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        self.conn.execute(sql)
        self._seed_competition()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def _seed_competition(self) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO competitions (competition, api_namespace, results_start)
            VALUES (?, ?, ?)
            """,
            [self.s.competition, self.s.competition, 2001],
        )

    # ------------------------------------------------------------- upserts
    def upsert_rows(self, table: str, rows: Iterable[dict]) -> None:
        """Insert-or-replace rows for a known table (columns from row keys)."""
        rows = list(rows)
        if not rows:
            return
        cols = list(rows[0].keys())
        qmarks = ",".join(["?"] * len(cols))
        quoted = ",".join(f'"{c}"' for c in cols)
        sql = f'INSERT OR REPLACE INTO "{table}" ({quoted}) VALUES ({qmarks})'
        self.conn.executemany(sql, [[r.get(c) for c in cols] for r in rows])

    # ------------------------------------------------------------ queries
    def execute(self, sql: str, params: Optional[list] = None) -> Any:
        return self.conn.execute(sql, params or [])

    def fetchall(self, sql: str, params: Optional[list] = None) -> list:
        return self.conn.execute(sql, params or []).fetchall()

    def fetchone(self, sql: str, params: Optional[list] = None):
        return self.conn.execute(sql, params or []).fetchone()

    # ------------------------------------------------------- raw responses
    def record_raw(self, competition: str, tool: str, raw_key: str, path: str,
                   credits: int, size: int) -> None:
        self.upsert_rows("raw_api_responses", [{
            "competition": competition,
            "tool": tool,
            "raw_key": raw_key,
            "path": path,
            "credits": credits,
            "size_bytes": size,
        }])

    def raw_exists(self, competition: str, tool: str, raw_key: str) -> bool:
        row = self.fetchone(
            "SELECT 1 FROM raw_api_responses WHERE competition=? AND tool=? AND raw_key=?",
            [competition, tool, raw_key])
        return row is not None

    def raw_path(self, competition: str, tool: str, raw_key: str) -> Optional[str]:
        row = self.fetchone(
            "SELECT path FROM raw_api_responses WHERE competition=? AND tool=? AND raw_key=?",
            [competition, tool, raw_key])
        return row[0] if row else None

    # ----------------------------------------------------------- usage ledger
    def bump_usage(self, month: str, tool: str, credits: int) -> None:
        cur = self.fetchone(
            "SELECT credits, calls FROM api_usage WHERE month=? AND tool=?",
            [month, tool])
        if cur:
            self.execute(
                "UPDATE api_usage SET credits=credits+?, calls=calls+1 WHERE month=? AND tool=?",
                [credits, month, tool])
        else:
            self.upsert_rows("api_usage", [{"month": month, "tool": tool,
                                            "credits": credits, "calls": 1}])

    def month_spent(self, month: str) -> int:
        row = self.fetchone("SELECT COALESCE(SUM(credits),0) FROM api_usage WHERE month=?",
                            [month])
        return int(row[0]) if row else 0

    def all_usage(self) -> list:
        return self.fetchall(
            "SELECT month, tool, credits, calls FROM api_usage ORDER BY month, tool")

    # ------------------------------------------------------------ checkpoints
    def upsert_checkpoint(self, row: dict) -> None:
        self.upsert_rows("ingestion_checkpoints", [row])

    def checkpoints(self, status=None) -> list:
        sql = ("SELECT competition, season, game_id, starts_at, ingestion_status, "
               "attempts, last_attempt, completed_at, error_message "
               "FROM ingestion_checkpoints")
        if status:
            sql += " WHERE ingestion_status=?"
        return self.fetchall(sql, [status] if status else None)

    # ------------------------------------------------------------- reporting
    def summary_table(self) -> dict:
        out = {}
        for name in ("games", "team_match_stats", "player_match_stats",
                     "odds_books", "elo_snapshots"):
            try:
                row = self.fetchone(f"SELECT count(*) FROM {name}")
                n = row[0] if row else 0
            except Exception:
                n = 0
            out[name] = n
        return out

    def new_job(self, competition, season, mode) -> str:
        job_id = str(uuid.uuid4())
        self.upsert_rows("ingestion_jobs", [{
            "job_id": job_id, "started_at": None, "finished_at": None,
            "competition": competition, "season": season, "mode": mode,
            "status": "running", "games_found": 0, "games_done": 0,
            "games_skipped": 0, "games_failed": 0, "credits_used": 0, "message": None,
        }])
        return job_id

    def finish_job(self, job_id: str, **kwargs) -> None:
        allowed = {"status", "games_found", "games_done", "games_skipped",
                   "games_failed", "credits_used", "message"}
        sets, params = [], []
        for k, v in kwargs.items():
            if k in allowed:
                sets.append(f"{k}=?")
                params.append(v)
        if kwargs.get("finished") is not None or "finished" in kwargs:
            sets.append("finished_at=now()")
        if sets:
            params.append(job_id)
            self.execute(
                f"UPDATE ingestion_jobs SET {', '.join(sets)} WHERE job_id=?",
                params)