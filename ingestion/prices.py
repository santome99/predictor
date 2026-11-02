"""Quota/odds conversions. All API odds are American-style odds (e.g. -118, 105, 250).

Preserves the original value (NULL stays NULL) and adds a decimal equivalent.
"""
from __future__ import annotations

from typing import Optional, Union

_Number = Union[int, float, str, None]


def american_to_decimal(american: _Number) -> Optional[float]:
    """Convert American odds to decimal odds.

    Rules:
      - positive american a  -> 1 + a / 100
      - negative american a  -> 1 + 100 / abs(a)
      - zero / None / ''     -> None (no convertible, no 0)

    Examples: 105 -> 2.05 ; -118 -> 1.8477... ; 250 -> 3.5 ; -250 -> 1.4
    Raises ValueError for clearly invalid non-numeric input.
    """
    if american is None:
        return None
    if isinstance(american, str):
        s = american.strip()
        if not s or s.lower() in {"null", "none", "nan"}:
            return None
        try:
            american = float(s)
        except ValueError:
            raise ValueError(f"invalid american odds: {american!r}")
    american = float(american)
    if american == 0.0:
        return None
    if american > 0:
        return round(1.0 + american / 100.0, 4)
    return round(1.0 + 100.0 / abs(american), 4)


def decimal_to_probability(decimal: Optional[float]) -> Optional[float]:
    """Implied probability from decimal odds (1 / decimal), no-margin passthrough."""
    if decimal is None or decimal <= 1.0:
        return None
    return round(1.0 / decimal, 6)


def is_nullish(value: _Number) -> bool:
    return value is None or (isinstance(value, str) and value.strip() in {"", "null", "None", "nan", "{}"})