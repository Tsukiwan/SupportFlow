"""Money constraints: Decimal only, at most two decimal places, no floats."""

from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from app.domain.money import Money, PositiveMoney


class Wallet(BaseModel):
    amount: Money


class PositiveWallet(BaseModel):
    amount: PositiveMoney


def test_decimal_amount_is_accepted():
    wallet = Wallet(amount=Decimal("12.34"))
    assert wallet.amount == Decimal("12.34")
    assert isinstance(wallet.amount, Decimal)


def test_int_and_numeric_string_are_accepted():
    assert Wallet(amount=42).amount == Decimal("42")
    assert Wallet(amount="9.99").amount == Decimal("9.99")


@pytest.mark.parametrize("bad", ["10.999", "-0.01", "-5"])
def test_too_many_decimal_places_and_negative_are_rejected(bad):
    with pytest.raises(ValidationError):
        Wallet(amount=bad)


@pytest.mark.parametrize("bad", [0.1, 10.5, -1.0])
def test_float_is_rejected_even_if_representable(bad):
    with pytest.raises(ValidationError):
        Wallet(amount=bad)


def test_zero_is_allowed_for_plain_money():
    assert Wallet(amount=0).amount == Decimal("0")


def test_zero_is_rejected_for_positive_money():
    with pytest.raises(ValidationError):
        PositiveWallet(amount=0)


def test_positive_money_accepts_smallest_unit():
    assert PositiveWallet(amount=Decimal("0.01")).amount == Decimal("0.01")
