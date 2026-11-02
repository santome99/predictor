"""Local credit budget controller.

The API does not document a balance endpoint, so we keep a local ledger in the
DB (api_usage) and protect the monthly budget locally.
"""
from __future__ import annotations

import datetime as dt
import logging

from ingestion.config import Settings
from ingestion.db import Database

log = logging.getLogger("strikerapi.credits")


class BudgetController:
    def __init__(self, settings: Settings, db: Database):
        self.s = settings
        self.db = db
        self.month = dt.date.today().strftime("%Y-%m")

    # ------------------------------------------------------------- queries
    def spent_this_month(self) -> int:
        return self.db.month_spent(self.month)

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
        """Registrar gasto real. Marca intentos de sobrepasar la reserva."""
        self.db.bump_usage(self.month, tool, credits)
        if self.available() <= 0:
            log.warning(
                "presupuesto efectivo agotado (gastado=%s, cap=%s, reserva=%s)",
                self.spent_this_month(), self.effective_cap(), self.reserve())

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