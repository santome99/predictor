"""Feature engineering + prediction layer.

Consume data from the DuckDB ingested by ``ingestion.*`` and produce:
  1. One-row-per-match feature matrices (train: past finals; predict: fixtures)
  2. Models: bivariate Poisson (goals) + histogram GBM (1X2) + blend
  3. Walk-forward backtest with EV/ROI vs closing odds
  4. Probabilities for upcoming fixtures

Decision log (reasons for every design choice): DECISIONES_FEATURES.md
"""
__version__ = "0.1.0"