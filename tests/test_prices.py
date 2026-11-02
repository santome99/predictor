"""Tests: American->decimal odds conversion."""
import pytest

from ingestion.prices import american_to_decimal, decimal_to_probability


def test_positive():
    assert american_to_decimal(105) == pytest.approx(2.05)
    assert american_to_decimal(250) == pytest.approx(3.50)
    assert american_to_decimal(100) == pytest.approx(2.00)


def test_negative():
    assert american_to_decimal(-118) == pytest.approx(1.8475, rel=1e-4)
    assert american_to_decimal(-250) == pytest.approx(1.40)
    assert american_to_decimal(-108) == pytest.approx(1 + 100 / 108, rel=1e-4)


def test_none():
    assert american_to_decimal(None) is None
    assert american_to_decimal("") is None
    assert american_to_decimal(0) is None
    assert american_to_decimal("null") is None


def test_string_input():
    assert american_to_decimal("105") == 2.05
    assert american_to_decimal("-118") == pytest.approx(1.8475, rel=1e-4)


def test_invalid_raises():
    with pytest.raises(ValueError):
        american_to_decimal("abc")


def test_probability():
    assert decimal_to_probability(2.0) == 0.5
    assert decimal_to_probability(None) is None
    assert decimal_to_probability(1.0) is None  # no margin