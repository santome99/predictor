"""Configuration: reads settings from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str = "https://mcp.strikerapi.com"
    competition: str = "laliga"
    db_path: Path = ROOT / "curated" / "striker.db"
    raw_dir: Path = ROOT / "raw"
    monthly_budget: int = 10_000
    reserve_credits: int = 500
    # Saldo REAL disponible ahora mismo (0 = desconocido). Si se informa, el
    # tope de gasto y el frenado por rate-limit usan este valor en vez del
    # presupuesto mensual: con 498 creditos no tiene sentido planificar 10.000.
    credits_remaining: int = 0
    rate_per_minute: int = 60          # 60 req/min cuando saldo >= 100 creditos
    low_balance_rate_per_minute: int = 10
    low_balance_threshold: int = 100
    timeout_seconds: float = 30.0
    max_retries: int = 4               # reintentos ante 5xx/429 (con backoff)
    retry_backoff_base: float = 2.0
    # Costes documentados por endpoint (creditos)
    endpoint_cost: tuple = (
        ("list_games", 2),
        ("get_game", 5),
        ("get_ratings", 2),
        ("get_odds", 5),
        ("get_lineups", 5),
        ("get_events", 10),
        ("get_line_movement", 10),
        ("get_player_props", 10),
        ("list_teams", 1),
        ("search_players", 1),
        ("get_player", 5),
        ("get_player_games", 5),
        ("get_schedule", 2),
        ("get_standings", 2),
        ("get_head_to_head", 2),
        ("get_team_stats", 5),
        ("get_leaders", 5),
        ("get_shot_map", 10),
        ("get_prop_history", 10),
    )

    @property
    def endpoint_costs(self) -> dict:
        return dict(self.endpoint_cost)

    def cost_of(self, tool: str, default: int = 5) -> int:
        return self.endpoint_costs.get(tool, default)


def load_settings() -> Settings:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")  # no sobreescribe variables ya presentes
    p = Path(os.getenv("STRIKER_DB_PATH", ROOT / "curated" / "striker.db")).expanduser()
    raw = Path(os.getenv("STRIKER_RAW_DIR", ROOT / "raw")).expanduser()
    return Settings(
        api_key=os.getenv("STRIKER_API_KEY", "").strip(),
        base_url=os.getenv("STRIKER_BASE_URL", "https://mcp.strikerapi.com").rstrip("/"),
        competition=os.getenv("STRIKER_COMPETITION", "laliga"),
        db_path=p,
        raw_dir=raw,
        monthly_budget=int(os.getenv("STRIKER_MONTHLY_BUDGET", "10000")),
        reserve_credits=int(os.getenv("STRIKER_RESERVE_CREDITS", "500")),
        credits_remaining=int(os.getenv("STRIKER_CREDITS_REMAINING", "0") or "0"),
        rate_per_minute=int(os.getenv("STRIKER_RATE_PER_MINUTE", "60")),
        low_balance_rate_per_minute=int(os.getenv("STRIKER_LOW_BALANCE_RATE", "10")),
        low_balance_threshold=int(os.getenv("STRIKER_LOW_BALANCE_THRESHOLD", "100")),
        timeout_seconds=float(os.getenv("STRIKER_TIMEOUT", "30")),
        max_retries=int(os.getenv("STRIKER_MAX_RETRIES", "4")),
        retry_backoff_base=float(os.getenv("STRIKER_BACKOFF_BASE", "2")),
    )