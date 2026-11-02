"""Odds intake: parse user-provided odds (e.g. pasted from a bookmaker page).

DECISION_D25 — Do NOT scrape bookmaker SPA pages (Winamax etc.). The odds are
rendered client-side via private JS endpoints: scraping is fragile, breaks
weekly, and touches ToS issues. Instead the user pastes a small JSON with the
markets they SEE on the page (or a future optional adapter plugs a licensed
odds API). This module is the intake parser.

Supported market shapes (per fixture):
    "1x2":        {"home": 2.40, "draw": 3.50, "away": 2.90}
    "over_under": {"line": 2.5, "over": 1.90, "under": 1.95}
    "btts":       {"yes": 1.85, "no": 1.95}

Example file:
{
  "bookmaker": "winamax",
  "matches": [
    {"home": "Real Madrid", "away": "Sevilla",
     "markets": {
        "1x2": {"home": 1.55, "draw": 4.2, "away": 6.8},
        "over_under": {"line": 2.5, "over": 1.9, "under": 1.95},
        "btts": {"yes": 1.8, "no": 2.0}}}
  ]
}
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Optional

import pandas as pd

SELECTIONS = {
    "1x2": ["home", "draw", "away"],
    "over_under": ["over", "under"],
    "btts": ["yes", "no"],
}


def normalize_name(name: str) -> str:
    """Fold team names for robust matching: lowercase, strip accents and
    punctuation, collapse whitespace. 'Atlético de Madrid' -> 'atletico de madrid'"""
    n = unicodedata.normalize("NFKD", str(name))
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def parse_odds_text(text: str) -> dict:
    """Parse pasted JSON (or path to a JSON file)."""
    t = text.strip()
    if not t.startswith("{"):
        p = Path(t)
        if not p.exists():
            raise ValueError(f"no es JSON ni ruta válida: {text[:60]!r}")
        t = p.read_text(encoding="utf-8")
    data = json.loads(t)
    if not isinstance(data, dict) or "matches" not in data:
        raise ValueError("formato esperado: {'bookmaker': ..., 'matches': [...]}")
    return data


class OddsIntake:
    """Normalised view of pasted odds, ready to be scored by the model."""

    def __init__(self, payload: dict):
        self.bookmaker = payload.get("bookmaker", "manual")
        self.rows = self._flatten(payload)

    @staticmethod
    def _flatten(payload: dict) -> pd.DataFrame:
        rows = []
        for i, m in enumerate(payload.get("matches") or []):
            markets = m.get("markets") or {}
            for market, sel in markets.items():
                if market not in SELECTIONS or not isinstance(sel, dict):
                    raise ValueError(
                        f"mercado desconocido en partido {i}: {market!r}")
                for key in SELECTIONS[market]:
                    odds = sel.get(key)
                    if odds is None:
                        continue
                    rows.append({
                        "fixture_idx": i,
                        "home_raw": m.get("home"),
                        "away_raw": m.get("away"),
                        "date": m.get("date"),
                        "market": market,
                        "line": sel.get("line") if market == "over_under" else None,
                        "selection": key,
                        "odds_dec": float(odds),
                    })
        df = pd.DataFrame(rows)
        if df.empty:
            raise ValueError("no hay mercados válidos en el JSON")
        return df


class TeamResolver:
    """Map pasted team names to known team_ids from the ingested teams table."""

    def __init__(self, teams: pd.DataFrame):
        # teams df: competition, team_id, abbrev, name
        self.known: dict = {}     # normalised alias -> team_id
        for _, r in teams.iterrows():
            aliases = {r.get("name"), r.get("abbrev"), r.get("display_name")}
            for a in aliases:
                if a and str(a).strip():
                    self.known.setdefault(normalize_name(str(a)), str(r["team_id"]))

    def resolve(self, name: str) -> Optional[str]:
        norm = normalize_name(name)
        if norm in self.known:
            return self.known[norm]
        # contains-match fallback (e.g. pasted "Real Madrid CF" vs "Real Madrid")
        for alias, tid in self.known.items():
            if alias and (alias in norm or norm in alias):
                return tid
        return None