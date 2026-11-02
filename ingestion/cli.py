"""CLI: `python -m ingestion <command>`."""
from __future__ import annotations

import argparse
import logging
import sys

from ingestion.config import Settings, load_settings
from ingestion.db import Database
from ingestion.client import CreditsExhaustedError


def _make(args, need_key: bool = False) -> tuple[Settings, Database]:
    settings = load_settings()
    if need_key and not settings.api_key:
        logging.error("STRIKER_API_KEY no está definida (.env). Abortando.")
        raise SystemExit(1)
    db = Database(settings)
    return settings, db


def cmd_status(args) -> int:
    settings, db = _make(args)
    from ingestion import report
    db.migrate()
    print(report.render_status(report.build_status(db, args.competition)))
    db.close()
    return 0


def cmd_budget(args) -> int:
    settings, db = _make(args)
    db.migrate()
    from ingestion import report
    from ingestion.credits import BudgetController
    budget = BudgetController(settings, db)
    b = report.build_budget_report(db, settings)
    b["available"] = budget.available()
    b["spent_this_month"] = budget.spent_this_month()
    b["usage_by_tool"] = db.all_usage()
    print(report.render_budget(b))
    db.close()
    return 0


def _ingestor(args):
    settings, db = _make(args, need_key=True)
    db.migrate()
    from ingestion.client import StrikerClient
    from ingestion.ingestor import Ingestor
    from ingestion.credits import BudgetController
    client = StrikerClient(settings)
    budget = BudgetController(settings, db)
    ing = Ingestor(settings, client, db, budget)
    return settings, db, client, budget, ing


def cmd_run(args) -> int:
    settings, db, client, budget, ing = _ingestor(args)
    seasons = args.season or [2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017]
    total_done = 0
    try:
        for season in seasons:
            print(f"[season {season}] descubriendo partidos finales...", flush=True)
            games = ing.discover_season(args.competition, season)
            n = len(games)
            pending = _count_pending(db, args.competition, season)
            print(f"[season {season}] {n} finales, {pending} pendientes", flush=True)
            # preflight estimado: get_game por partido pendiente + coste descubrimiento
            est = pending * settings.cost_of("get_game") + 20
            if not budget.can_afford(est):
                print(f"[season {season}] presupuesto insuficiente (~{est} cr < disponible {budget.available()}). Paro.", flush=True)
                break
            total_done += ing.process_games(games, args.competition, mode="run",
                                            max_games=args.limit)
            print(f"[season {season}] hechos {total_done}", flush=True)
            if args.limit and total_done >= args.limit:
                print(f"[limit] alcanzado {args.limit}. Paro.", flush=True)
                break
    except CreditsExhaustedError as exc:
        print(f"[budget] creditos agotados (402/deteccion local): {exc}. Ingesta pausada.", flush=True)
        db.close()
        return 0
    except Exception as exc:  # noqa: BLE001
        logging.error("ingesta abortada: %s", exc)
        db.close()
        return 1
    db.close()
    print(f"OK: {total_done} partidos completados")
    return 0


def cmd_resume(args) -> int:
    settings, db, client, budget, ing = _ingestor(args)
    row = db.fetchone(
        f"SELECT MAX(season) FROM games WHERE competition=? AND status='final'",
        [args.competition])
    season = row[0] if row else None
    pend = db.checkpoints("FAILED") + db.checkpoints("RETRY_PENDING")
    print(f"Resume: temporada actual={season}, pendientes={len(pend)}")
    # reencola: marcar RETRY_PENDING -> PENDING y relanzar
    db.execute(
        "UPDATE ingestion_checkpoints SET ingestion_status='PENDING' "
        "WHERE competition=? AND ingestion_status IN ('RETRY_PENDING','FAILED')",
        [args.competition])
    games = db.fetchall(
        "SELECT game_id, season, starts_at FROM games WHERE competition=? "
        "AND status='final' AND game_id IN "
        "(SELECT game_id FROM ingestion_checkpoints WHERE ingestion_status='PENDING') "
        "ORDER BY starts_at DESC LIMIT ?", [args.competition, args.limit])
    game_rows = [{"game_id": g[0], "season": g[1], "starts_at": g[2],
                  "competition": args.competition} for g in games]
    done = ing.process_games(game_rows, args.competition, mode="resume")
    db.close()
    print(f"OK: {done} reanudados")
    return 0


def cmd_retry(args) -> int:
    settings, db, client, budget, ing = _ingestor(args)
    db.execute(
        "UPDATE ingestion_checkpoints SET ingestion_status='PENDING' "
        "WHERE competition=? AND error_message IS NOT NULL "
        "AND attempts < ?", [args.competition, settings.max_retries + 1])
    pend = db.fetchone(
        "SELECT count(*) FROM ingestion_checkpoints WHERE competition=? "
        "AND ingestion_status='PENDING'", [args.competition])
    print(f"reintentos reencolados: {pend[0] if pend else 0}")
    db.close()
    return 0


def cmd_report(args) -> int:
    settings, db = _make(args)
    db.migrate()
    from ingestion import report
    from ingestion.credits import BudgetController
    st = report.build_status(db, args.competition)
    bg = BudgetController(settings, db)
    print(report.render_status(st))
    print("\n--- Presupuesto ---")
    print(report.render_budget(report.build_budget_report(db, settings)))
    db.close()
    return 0


def cmd_dry_run(args) -> int:
    settings, db = _make(args)
    db.migrate()
    from ingestion.client import StrikerClient
    from ingestion.credits import BudgetController
    client = StrikerClient(settings)
    budget = BudgetController(settings, db)
    # simula descubrimiento SIN llamada: estima por temporada
    seasons = args.season or [2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017]
    cost = 0
    for season in seasons:
        # list_games: paginas estimadas ~8 (limit 50) x 2
        list_cost = 8 * settings.cost_of("list_games")
        # get_game por partido: estimacion por temporada segun promedio historico
        est_games = args.assume_games if args.assume_games else 380
        game_cost = est_games * settings.cost_of("get_game")
        cost += list_cost + game_cost
        print(f"[{season}] list_games≈{list_cost} cr + get_game≈{game_cost} cr "
              f"(asumiendo {est_games} finales)")
    print(f"\nCoste estimado total: {cost} cr")
    print(f"Disponible este mes: {budget.available()} cr (cap {budget.effective_cap()})")
    db.close()
    return 0


def _count_pending(db, competition, season) -> int:
    row = db.fetchone(
        "SELECT count(*) FROM ingestion_checkpoints WHERE competition=? "
        "AND season=? AND ingestion_status IN ('PENDING','FAILED','RETRY_PENDING')",
        [competition, season])
    return row[0] if row else 0


def cmd_elo(args) -> int:
    settings, db, client, budget, ing = _ingestor(args)
    seasons = args.season or [2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017]
    total = 0
    for season in seasons:
        try:
            n = ing.ingest_elo_for_season(args.competition, season)
            total += n
            print(f"[elo {season}] {n} filas de rating", flush=True)
        except CreditsExhaustedError as e:
            print(f"[elo {season}] 402 -> detengo: {e}", flush=True)
            break
        except Exception as exc:  # noqa: BLE001
            logging.error("elo temporada %s fallida: %s", season, exc)
    db.close()
    print(f"OK: {total} ratings registrados")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ingestion", description="StrikerAPI historic ingestion")
    parser.add_argument("--competition", default=None, help="laliga | ucl (def: env)")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="estado de la ingesta")
    p = sub.add_parser("budget", help="presupuesto")
    p = sub.add_parser("run", help="descender temporadas y descargar")
    p.add_argument("--season", type=int, action="append", help="una temporada (año inicio)")
    p.add_argument("--limit", type=int, default=0,
                   help="parar tras N partidos procesados (0 = sin límite; útil para pruebas)")
    p = sub.add_parser("resume", help="reanudar")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("retry", help="reencolar fallidos")
    p = sub.add_parser("elo", help="descargar ratings (get_ratings) por fecha")
    p.add_argument("--season", type=int, action="append")
    p = sub.add_parser("report", help="resumen")
    p = sub.add_parser("dry-run", help="estimar coste sin llamadas")
    p.add_argument("--season", type=int, action="append")
    p.add_argument("--assume-games", type=int, default=380,
                   help="partidos finales asumidos por temporada")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # competition default desde env ya lo resuelve _make; aqui lo propagamos
    if args.competition is None:
        args.competition = load_settings().competition

    funcs = {
        "status": cmd_status, "budget": cmd_budget, "run": cmd_run,
        "resume": cmd_resume, "retry": cmd_retry, "report": cmd_report,
        "dry-run": cmd_dry_run, "elo": cmd_elo,
    }
    sys.exit(funcs[args.cmd](args))