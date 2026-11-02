"""End-to-end features pipeline test on the synthetic demo DB."""
import pandas as pd
import pytest


@pytest.fixture(scope="session")
def demo_db(tmp_path_factory):
    from features.demo import build_demo_db
    p = tmp_path_factory.mktemp("demo") / "demo.db"
    build_demo_db(p)
    return p


@pytest.fixture(scope="session")
def demo_artifacts(tmp_path_factory):
    return tmp_path_factory.mktemp("artifacts")


def _cfg(demo_db, demo_artifacts, monkeypatch):
    from features.config import FeatureConfig
    monkeypatch.setenv("STRIKER_DB_PATH", str(demo_db))
    monkeypatch.setenv("STRIKER_COMPETITION", "demo")
    monkeypatch.setenv("FEATURES_ARTIFACTS_DIR", str(demo_artifacts))
    cfg = FeatureConfig(db_path=demo_db, artifacts_dir=demo_artifacts,
                        competition="demo")
    return cfg


def test_build_backtest_predict(demo_db, demo_artifacts, monkeypatch):
    cfg = _cfg(demo_db, demo_artifacts, monkeypatch)
    from features.builder import MatrixBuilder

    builder = MatrixBuilder(cfg)
    try:
        matrix = builder.build()
    finally:
        builder.close()

    assert len(matrix) == 528          # 4 seasons × 132 finals
    assert "home_win" in matrix.columns
    assert "p_home_close" in matrix.columns
    assert "h_lag_goals_for" in matrix.columns

    # training rows must not include fixtures (only finals)
    assert set(matrix["status"].unique()) == {"final"}

    # market features sanity: no-margin probs must sum to 1
    for _, row in matrix.iterrows():
        s = row["p_home_close"] + row["p_draw_close"] + row["p_away_close"]
        assert abs(s - 1.0) < 0.05 or pd.isna(row["p_home_close"])

    # backtest: walk-forward by season has no temporal overlap
    from features.backtest import run_backtest
    summary = run_backtest(matrix, cfg, cfg.artifacts_dir / "wf.parquet")
    assert summary["ok"] is True
    assert summary["seasons"] >= 2

    # predictions for scheduled fixtures, no target needed
    import duckdb
    from features.models import GBMResultModel, PoissonGoalsModel
    from features.predict import (build_fixture_features, load_fixtures,
                                  load_past_log, predict_fixtures)
    conn = duckdb.connect(str(cfg.db_path), read_only=True)
    fixtures = load_fixtures(cfg, conn)
    past = load_past_log(cfg, conn)
    tms = conn.execute("SELECT * FROM team_match_stats").df()
    elo = conn.execute(
        "SELECT competition, team_id, as_of, elo, rank, last_game"
        " FROM elo_snapshots ORDER BY team_id, as_of").df()
    conn.close()

    assert len(fixtures) == 12
    fx = build_fixture_features(cfg, fixtures, past, tms, elo)
    poisson = PoissonGoalsModel(max_goals=8).fit(matrix)
    gbm = GBMResultModel(cfg).fit(matrix)
    out = predict_fixtures(cfg, fx, matrix, poisson, gbm)
    assert len(out) == 12
    for _, r in out.iterrows():
        tot = r["p_home"] + r["p_draw"] + r["p_away"]
        assert abs(tot - 1.0) < 1e-6
        assert 0 <= r["p_over_2_5"] <= 1
        assert 0 <= r["p_btts"] <= 1