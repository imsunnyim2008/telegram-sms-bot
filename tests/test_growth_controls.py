from decimal import Decimal

from app.growth_controls import margin_discount


def test_promo_never_reduces_provider_cost():
    assert margin_discount(Decimal('3'), Decimal('2'), Decimal('50')) == Decimal('2.5')
    assert margin_discount(Decimal('3'), Decimal('2'), Decimal('100')) == Decimal('2')
    assert margin_discount(Decimal('3'), Decimal('2'), Decimal('101')) == Decimal('3')
