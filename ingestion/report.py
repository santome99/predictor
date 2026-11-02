"""Reports: ingest status and per-execution summary."""
from __future__ import annotations

import datetime as dt

from ingestion.config import Settings
from ingestion.db import Database


from typing import Optional


def _row(query_row):
    if query_row:
        val = query_row[0]
        if isinstance(val, (list, tuple)):
            val = val[0]
        if val is not None:
            return val
    return 0


def build_status(db: Database, competition: Optional[str] = None) -> dict:
    comp = competition or "laliga"
    games = db.fetchall(
        "SELECT count(*) FROM games WHERE competition=?", [comp])
    finals = db.fetchall(
        "SELECT count(*) FROM games WHERE competition=? AND status='final'", [comp])
    stats = db.fetchall(
        "SELECT count(*) FROM team_match_stats t JOIN games g ON "
        "t.competition=g.competition AND t.game_id=g.game_id "
        "WHERE t.competition=? AND g.status='final'", [comp])
    odds = db.fetchall(
        "SELECT count(DISTINCT o.game_id) FROM odds_books o JOIN games g ON "
        "o.competition=g.competition AND o.game_id=g.game_id "
        "WHERE o.competition=? AND g.status='final'", [comp])
    ml = db.fetchall(
        "SELECT count(DISTINCT o.game_id) FROM odds_books o JOIN games g ON "
        "o.competition=g.competition AND o.game_id=g.game_id "
        "WHERE o.competition=? AND o.home_ml_close_orig IS NOT NULL AND g.status='final'",
        [comp])
    openclose = db.fetchall(
        "SELECT count(DISTINCT o.game_id) FROM odds_books o JOIN games g ON "
        "o.competition=g.competition AND o.game_id=g.game_id "
        "WHERE o.competition=? AND o.home_ml_open_orig IS NOT NULL "
        "AND o.home_ml_close_orig IS NOT NULL AND g.status='final'", [comp])
    avg_books = db.fetchall(
        "SELECT avg(nb) FROM (SELECT count(DISTINCT provider) nb FROM odds_books "
        "WHERE competition=? GROUP BY game_id)", [comp])
    cps = db.checkpoints()
    by_status = {}
    for cp in cps:
        st = cp[4]
        by_status[st] = by_status.get(st, 0) + 1

    return {
        "competition": comp,
        "games_total": _row(games),
        "games_final": _row(finals),
        "games_with_stats": _row(stats),
        "games_with_odds": _row(odds),
        "games_with_close": _row(ml),
        "games_with_open_close": _row(openclose),
        "avg_books_per_match": round(_row(avg_books), 2) if _row(avg_books) else 0.0,
        "checkpoints": by_status,
        "tables": db.summary_table(),
    }


def build_budget_report(db: Database, settings: Settings) -> dict:
    month = dt.date.today().strftime("%Y-%m")
    spent = db.month_spent(month)
    caps = [settings.monthly_budget]
    if settings.credits_remaining > 0:
        caps.append(settings.credits_remaining)
    total_cap = min(caps)
    effective = max(0, total_cap - settings.reserve_credits)
    return {
        "month": month,
        "monthly_budget": settings.monthly_budget,
        "credits_remaining": settings.credits_remaining,
        "total_cap": total_cap,
        "reserve_credits": settings.reserve_credits,
        "effective_cap": effective,
        "spent_this_month": spent,
        "available": max(0, effective - spent),
        "usage_by_tool": db.all_usage(),
    }


def render_status(status: dict) -> str:
    lines = [
        f"Competition           : {status['competition']}",
        f"Games (total)         : {status['games_total']}",
        f"Games (final)         : {status['games_final']}",
        f"Games with stats      : {status['games_with_stats']}",
        f"Games with odds       : {status['games_with_odds']}",
        f"  ... with close      : {status['games_with_close']}",
        f"  ... with open+close : {status['games_with_open_close']}",
        f"Avg books/match       : {status['avg_books_per_match']}",
        "Checkpoints:",
    ]
    for st in ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "RETRY_PENDING"):
        lines.append(f"  {st:<14}: {status['checkpoints'].get(st, 0)}")
    lines.append("Table row counts:")
    for k, v in status["tables"].items():
        lines.append(f"  {k:<22}: {v}")
    return "\n".join(lines)


def render_budget(b: dict) -> str:
    lines = [
        f"Month                 : {b['month']}",
        f"Monthly budget        : {b['monthly_budget']}",
        f"Credits remaining now : {b['credits_remaining'] or 'desconocido'}",
        f"Total cap (min budget,remaining): {b['total_cap']}",
        f"Reserve credits       : {b['reserve_credits']}",
        f"Effective cap         : {b['effective_cap']}",
        f"Spent this month      : {b['spent_this_month']}",
        f"Available (to reserve): {b['available']}",
        "Usage by tool:",
    ]
    for month, tool, credits, calls in b["usage_by_tool"]:
        lines.append(f"  {month} {tool:<20}: {credits} cr / {calls} calls")
    return "\n".join(lines)