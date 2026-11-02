"""Configuration for the features layer.

DECISION_D1 — Single source of config via environment variables, mirroring
``ingestion.config``. All paths resolve relative to the repo root, so the
homelab can run from cron without a shell profile.

DECISION_D2 — Artifacts (matrices, models, reports) go to ``artifacts/``, NOT
inside the ``features/`` package: the package is code (versioned, read-only at
runtime) and artifacts are data derived from both raw JSON and DuckDB. Keeping
them apart means "rebuild artifacts" is always safe and never touches code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ingestion.config import ROOT

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # pragma: no cover
    pass


@dataclass(frozen=True)
class FeatureConfig:
    competition: str = os.getenv("STRIKER_COMPETITION", "laliga")
    db_path: Path = Path(os.getenv("STRIKER_DB_PATH", ROOT / "curated" / "striker.db"))
    artifacts_dir: Path = Path(os.getenv("FEATURES_ARTIFACTS_DIR", ROOT / "artifacts"))

    # DECISION_D8: ventanas de forma corta y larga (5 y 10 partidos previos).
    windows: tuple = (5, 10)
    min_periods: int = 3            # DECISION_D9: mínimo de partidos para emitir media
    max_goals: int = 8              # DECISION_D13: truncado de la matriz de Poisson
    cross_over_lines: tuple = (2.5,)   # líneas O/U que predecimos por defecto

    # Modelos
    poisson_weight: float = 0.5     # peso Poisson/GBM del blend (vease D13)
    min_train_matches: int = 200    # minimo historico para entrenar GBM (D21)
    gbm_max_iter: int = 300
    gbm_learning_rate: float = 0.06
    gbm_max_leaf_nodes: int = 31

    # Backtest / apuestas
    ev_threshold: float = 0.0       # apuesta si p*cuota - 1 > umbral (D19)
    bet_stake: float = 1.0

    teams: dict = field(default_factory=dict)

    @property
    def matrix_path(self) -> Path:
        return self.artifacts_dir / "train_matrix.parquet"

    @property
    def features_meta_path(self) -> Path:
        return self.artifacts_dir / "feature_columns.json"

    @property
    def models_dir(self) -> Path:
        return self.artifacts_dir / "models"


def load_config() -> FeatureConfig:
    return FeatureConfig()