"""Local credit budget controller.

The API does not document a balance endpoint, so we keep a local ledger in the
DB (api_usage), scoped PER CREDENTIAL (API key): each key has its own free
allowance, so batches run with different keys must not share the spend counter.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging

from ingestion.config import Settings
from ingestion.db import Database

log = logging.getLogger("strikerapi.credits")


def credential_of(settings: Settings) -> str:
    """Identificador NO reversible de la API key usada en esta tanda.

    Preferimos ``STRIKER_CREDENTIAL_LABEL`` (p.ej. "tanda1") si está definido;
    si no, un hash corto de la clave (nunca se guarda la clave en claro).
    """
    label = (settings.credential_label or "").strip()
    if label:
        return label
    if not settings.api_key:
        return "no-key"
    return "key-" + hashlib.sha256(settings.api_key.encode()).hexdigest()[:10]


class BudgetController:
    def __init__(self, settings: Settings, db: Database):
        self.s = settings
        self.db = db
        self.month = dt.date.today().strftime("%Y-%m")
        self.credential = credential_of(settings)

    # ------------------------------------------------------------- queries
    def spent_this_month(self) -> int:
        """Gasto del mes SOLO para la credencial (clave) actual."""
        return self.db.month_spent(self.month, self.credential)

    def reserve(self) -> int:
        return self.s.reserve_credits

    def monthly_budget(self) -> int:
        return self.s.monthly_budget

    def total_cap(self) -> int:
        """Tope bruto de gasto del mes.

        Si se conoce el saldo real (``STRIKER_CREDITS_REMAINING``), el tope es
        el MENOR entre presupuesto mensual y saldo real: con 498 creditos no
        podemos planificar 10.000. Si no se informa, se usa el presupuesto.
        """
        caps = [self.s.monthly_budget]
        if self.s.credits_remaining > 0:
            caps.append(self.s.credits_remaining)
        return min(caps)

    def effective_cap(self) -> int:
        """Máximo gastable = tope - reserva (nunca tocar la reserva)."""
        return max(0, self.total_cap() - self.s.reserve_credits)

    def available(self) -> int:
        return max(0, self.effective_cap() - self.spent_this_month())

    def can_afford(self, cost: int) -> bool:
        return cost <= self.available()

    def estimate_remaining(self) -> int:
        # Estimacion: tope bruto menos lo gastado; reserva notificada aparte.
        return max(0, self.total_cap() - self.spent_this_month())

    # ------------------------------------------------------------ spending
    def spend(self, tool: str, credits: int) -> None:
        """Registrar gasto real en la credencial actual."""
        self.db.bump_usage(self.month, self.credential, tool, credits)
        if self.available() <= 0:
            log.warning(
                "presupuesto efectivo agotado en credencial %s (gastado=%s, cap=%s, reserva=%s)",
                self.credential, self.spent_this_month(), self.effective_cap(),
                self.reserve())

    def preflight(self, tool: str, cost: int) -> bool:
        """¿Podemos ejecutar una llamada de este endpoint?"""
        if not self.s.api_key:
            log.warning("STRIKER_API_KEY vacía — no ejecuto llamadas")
            return False
        if not self.can_afford(cost):
            log.warning(
                "presupuesto insuficiente para %s (%s cr, disponible %s) — detengo",
                tool, cost, self.available())
            return False
        return True