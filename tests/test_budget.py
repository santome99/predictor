"""Tests: budget cap with real remaining credits + rate-limit throttling."""
from ingestion.config import Settings
from ingestion.credits import BudgetController


def _settings(**kw):
    base = dict(api_key="k", monthly_budget=10000, reserve_credits=500)
    base.update(kw)
    return Settings(**base)


class FakeDB:
    def __init__(self, spent=0):
        self.spent = spent

    def month_spent(self, month):
        return self.spent

    def bump_usage(self, month, tool, credits):
        self.spent += credits

    def all_usage(self):
        return []


def test_cap_uses_real_remaining_when_lower():
    s = _settings(credits_remaining=498, reserve_credits=50)
    b = BudgetController(s, FakeDB())
    assert b.total_cap() == 498          # min(10000, 498)
    assert b.effective_cap() == 448      # 498 - 50 reserva
    assert b.available() == 448


def test_cap_is_monthly_budget_when_no_remaining():
    s = _settings(credits_remaining=0, reserve_credits=500)
    b = BudgetController(s, FakeDB())
    assert b.total_cap() == 10000
    assert b.effective_cap() == 9500


def test_cap_never_exceeds_remaining_even_after_spending():
    db = FakeDB()
    s = _settings(credits_remaining=498, reserve_credits=50)
    b = BudgetController(s, db)
    assert b.can_afford(448)
    assert not b.can_afford(449)
    # gastamos 300 -> disponible 148
    for _ in range(60):                  # 60 x get_game = 300
        b.spend("get_game", 5)
    assert b.available() == 148
    assert b.can_afford(148)
    assert not b.can_afford(149)


def test_throttle_rate_uses_remaining_not_spent():
    """Con 150 creditos al empezar: rapido; al bajar de 100 restantes: lento."""
    from ingestion.client import StrikerClient

    c = StrikerClient.__new__(StrikerClient)
    c.s = _settings(credits_remaining=150, rate_per_minute=60,
                    low_balance_rate_per_minute=10,
                    low_balance_threshold=100)
    c.local_spent = 0
    assert c._rate_per_minute() == 60          # quedan 150 >= 100
    c.local_spent = 60                         # quedan 90 < 100
    assert c._rate_per_minute() == 10
    c.local_spent = 200                        # saldo agotado
    assert c.remaining_credits() == 0
    assert c._rate_per_minute() == 10


def test_throttle_rate_unknown_balance_stays_fast():
    from ingestion.client import StrikerClient

    c = StrikerClient.__new__(StrikerClient)
    c.s = _settings(credits_remaining=0, rate_per_minute=60,
                    low_balance_rate_per_minute=10,
                    low_balance_threshold=100)
    c.local_spent = 5000                       # irrelevante si no hay saldo
    assert c.remaining_credits() is None
    assert c._rate_per_minute() == 60