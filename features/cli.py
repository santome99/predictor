"""CLI: `python -m features build|backtest|predict|demo|show`.

DECISION_D22 — the features stage is a separate CLI from ingestion: ingestion
is a crawler (needs network, credits), features is offline compute (DuckDB +
parquet). Different failure domains, different schedules in the homelab.
"""
from __future__ import annotations

import argparse
import json
import sys

from features.config import FeatureConfig, load_config
from features.builder import MatrixBuilder


def cmd_build(args) -> int:
    cfg = load_config()
    builder = MatrixBuilder(cfg)
    try:
        matrix = builder.build()
        meta = builder.save_matrix_and_meta(matrix)
        print(json.dumps(meta, indent=2))
        print(f"Matrix: {meta['rows']} filas × {meta['columns']} columnas"
              f" -> {cfg.matrix_path}")
        return 0
    finally:
        builder.close()


def cmd_backtest(args) -> int:
    cfg = load_config()
    from features.builder import load_saved_matrix
    from features.backtest import run_backtest

    matrix = load_saved_matrix(cfg)
    if matrix.empty:
        print("Matrix vacía: ejecuta primero `python -m features build`")
        return 2
    out_path = cfg.artifacts_dir / "walk_forward.parquet"
    summary = run_backtest(matrix, cfg, out_path)
    print(json.dumps(summary, indent=2))
    return 0


def cmd_predict(args) -> int:
    cfg = load_config()
    import duckdb

    conn = duckdb.connect(str(cfg.db_path), read_only=True)
    from features.predict import (build_fixture_features, load_fixtures,
                                  load_past_log, predict_fixtures,
                                  save_predictions)
    from features.builder import load_saved_matrix
    from features.models import GBMResultModel, PoissonGoalsModel
    import pandas as pd

    fixtures = load_fixtures(cfg, conn)
    if fixtures.empty:
        print("No hay fixtures programados en la BD (usa `python -m ingestion"
              " run` tras nuevo partido, o get_schedule con API key).")
        conn.close()
        return 2

    past = load_past_log(cfg, conn)
    tms = conn.execute(
        "SELECT competition, game_id, team_id, home, goals, xg,"
        " possession_pct, shots, shots_on_target, blocked_shots, corners,"
        " fouls, yellow_cards, red_cards, offsides, saves, passes,"
        " accurate_passes, crosses, accurate_crosses, long_balls,"
        " accurate_long_balls, tackles, effective_tackles, interceptions,"
        " clearances, penalty_goals, penalty_attempts"
        " FROM team_match_stats").df()
    elo = conn.execute(
        "SELECT competition, team_id, as_of, elo, rank, last_game"
        " FROM elo_snapshots ORDER BY team_id, as_of").df()
    conn.close()

    fx = build_fixture_features(cfg, fixtures, past, tms, elo)

    matrix = load_saved_matrix(cfg)
    if matrix.empty:
        print("Sin matriz entrenada: ejecuta `python -m features build` primero.")
        return 2
    train_last = matrix.sort_values("starts_at")
    poisson = PoissonGoalsModel(max_goals=cfg.max_goals).fit(train_last)
    gbm = None
    if len(train_last) >= cfg.min_train_matches:
        gbm = GBMResultModel(cfg).fit(train_last)
    out = predict_fixtures(cfg, fx, train_last, poisson, gbm)
    cols = ["game_id", "date", "starts_at", "home_team_id", "away_team_id",
            "p_home", "p_draw", "p_away", "pick", "lambda_home", "lambda_away",
            "p_btts", "p_over_2_5"]
    cols = [c for c in cols if c in out.columns]
    print(out[cols].round(3).to_string(index=False))
    save_predictions(out, cfg)
    return 0


def cmd_recommend(args) -> int:
    """Cuotas pegadas (JSON) + modelo -> picks ordenados."""
    cfg = load_config()
    import sys

    from features.builder import load_saved_matrix
    from features.intake import OddsIntake, TeamResolver, parse_odds_text
    from features.models import GBMResultModel, PoissonGoalsModel
    from features.recommend import Recommender
    import duckdb
    import pandas as _pd

    odds_text = args.odds_text
    if odds_text == "-":
        odds_text = sys.stdin.read()

    try:
        payload = parse_odds_text(odds_text)
    except Exception as e:
        print(f"No pude interpretar la entrada: {e}")
        return 2
    intake = OddsIntake(payload)

    matrix = load_saved_matrix(cfg)
    if matrix.empty:
        print("No hay matriz entrenada (python -m features build)")
        return 2

    conn = duckdb.connect(str(cfg.db_path), read_only=True)
    try:
        teams = conn.execute(
            "SELECT team_id, name, abbrev FROM teams").df()
    except Exception:
        teams = _pd.DataFrame(columns=["team_id", "name", "abbrev"])
    finally:
        conn.close()

    poisson = PoissonGoalsModel(max_goals=cfg.max_goals).fit(matrix)
    gbm = None
    if len(matrix) >= cfg.min_train_matches:
        try:
            gbm = GBMResultModel(cfg).fit(matrix)
        except Exception:
            gbm = None

    resolver = TeamResolver(teams)
    rec = Recommender(cfg, poisson, gbm)
    scored = rec.score(intake, resolver)
    if scored.empty:
        print("Ningún partido del fichero coincide con equipos conocidos "
              "(solo recomendamos partidos que el modelo puede puntuar).")
        return 2

    picks = rec.recommend(
        scored, target_odds=args.target_odds, tolerance=args.tolerance,
        min_ev=args.min_ev, criterion=args.criterion, top=args.top,
        single_fixture_limit=args.per_fixture)
    print(Recommender.render(picks))
    return 0


def cmd_demo(args) -> int:
    """Generar un dataset tiny determinista para ver el pipeline completo."""
    from features.demo import build_demo_db
    cfg = load_config()
    demo_db = cfg.artifacts_dir / "demo.db"
    demo_db.parent.mkdir(parents=True, exist_ok=True)
    build_demo_db(demo_db)
    print(f"Demo DB: {demo_db}")
    print("Uso: FEATURES_ARTIFACTS_DIR y STRIKER_DB_PATH apuntan al demo;")
    print("corre: python -m features build && python -m features backtest")
    return 0


def cmd_show(args) -> int:
    cfg = load_config()
    from features.builder import load_saved_matrix
    matrix = load_saved_matrix(cfg)
    if matrix.empty:
        print("Matrix vacía.")
        return 2
    print(matrix.head(10).to_string())
    print("\nColumnas:", len(matrix.columns), "Filas:", len(matrix))
    print("NaN ratio por columna (top 10 con más ausentes):")
    nan_ratio = matrix.isna().mean().sort_values(ascending=False).head(10)
    print(nan_ratio.round(3).to_string())
    return 0


def schedule_fixtures(args) -> int:
    """Obtener próximos fixtures via get_schedule (requiere API key/creditos).

    DECISION_D23 — Scheduled fixtures cost 2 credits per call; we refresh them
    into DuckDB so predictions work offline afterwards.
    """
    cfg = load_config()
    from ingestion.config import load_settings
    from ingestion.client import StrikerClient

    s = load_settings()
    if not s.api_key:
        print("STRIKER_API_KEY no definida.")
        return 1
    client = StrikerClient(s)
    payload = client.call("get_schedule", {"days": args.days})
    games = payload.get("games") or []
    import duckdb
    conn = duckdb.connect(str(cfg.db_path))
    for g in games:
        conn.execute(
            "INSERT OR REPLACE INTO games (competition, game_id, season, stage,"
            " date, starts_at, status, home_team_id, away_team_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [cfg.competition, g["game_id"], g.get("season"), g.get("stage"),
             g.get("date"), g.get("starts_at"), g.get("status") or "scheduled",
             (g.get("home") or {}).get("team_id"),
             (g.get("away") or {}).get("team_id")])
    conn.close()
    print(f"{len(games)} fixtures de próximos {args.days} días grabados.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="features")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build", help="construir matriz train (as-of, anti-fuga)")
    sub.add_parser("backtest", help="walk-forward por temporada + EV/ROI")
    sub.add_parser("predict", help="probabilidades para próximos fixtures")
    sub.add_parser("demo", help="generar dataset demo sintético")
    sub.add_parser("show", help="inspeccionar la matriz guardada")
    p = sub.add_parser("schedule", help="refrescar fixtures (requiere key)")
    p.add_argument("--days", type=int, default=7)
    p = sub.add_parser("recommend",
                       help="pick recomendado a partir de cuotas pegadas (JSON)")
    p.add_argument("odds_text", help="JSON con cuotas o '-' para leer stdin, "
                                     "o ruta a un archivo .json")
    p.add_argument("--target-odds", type=float, default=2.5)
    p.add_argument("--tolerance", type=float, default=0.15,
                   help="ventana de cuota aceptada (ej. 2.5 ± 0.15)")
    p.add_argument("--min-ev", type=float, default=0.0,
                   help="EV mínimo para considerar (def. 0: sin valor, sin pick)")
    p.add_argument("--criterion", choices=["value", "safety"], default="value",
                   help="ordenar por EV ('value') o por prob. del modelo ('safety')")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--per-fixture", action="store_true",
                   help="como máximo una selección por partido")

    args = parser.parse_args(argv)
    funcs = {
        "build": cmd_build, "backtest": cmd_backtest, "predict": cmd_predict,
        "demo": cmd_demo, "show": cmd_show, "schedule": schedule_fixtures,
        "recommend": cmd_recommend,
    }
    return funcs[args.cmd](args)