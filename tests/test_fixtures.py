"""Deterministic mock catalog: coverage, semantics, reproducibility."""

from decimal import Decimal

import pytest

from app.domain.enums import IssueType, OrderStatus, RiskLevel
from tests.fixtures.mock_data import (
    MISSING_ORDER_ID,
    SCENARIO_NAMES,
    SCENARIO_ORDERS,
    SCENARIO_TICKETS,
    build_scenario_orders,
    build_scenario_tickets,
    get_scenario,
)


def test_required_scenarios_present():
    assert set(SCENARIO_NAMES) == {
        "refundable",
        "already_refunded",
        "cancelled",
        "amount_mismatch",
        "delivered_not_received",
        "non_existent",
    }


def test_tickets_and_orders_are_linked():
    for name in SCENARIO_NAMES:
        ticket, order = get_scenario(name)
        assert ticket.order_id == (order.order_id if order else MISSING_ORDER_ID)


def test_refundable_scenario_shape():
    ticket, order = get_scenario("refundable")
    assert order.status is OrderStatus.DELIVERED
    assert order.refunded_amount == Decimal("0")
    assert ticket.claimed_amount == order.total_amount


def test_already_refunded_scenario_shape():
    _, order = get_scenario("already_refunded")
    assert order.refunded_amount == order.total_amount
    assert order.refundable_amount == Decimal("0")


def test_cancelled_scenario_shape():
    _, order = get_scenario("cancelled")
    assert order.status is OrderStatus.CANCELLED


def test_amount_mismatch_scenario_shape():
    ticket, order = get_scenario("amount_mismatch")
    assert ticket.claimed_amount != order.total_amount
    assert ticket.claimed_amount > order.total_amount


def test_delivered_not_received_scenario_shape():
    ticket, order = get_scenario("delivered_not_received")
    assert order.status is OrderStatus.DELIVERED
    assert order.delivered_at is not None
    assert ticket.issue_type is IssueType.DELIVERY_ISSUE
    assert ticket.risk_level is RiskLevel.HIGH


def test_non_existent_scenario_has_no_order():
    ticket, order = get_scenario("non_existent")
    assert ticket.order_id == MISSING_ORDER_ID
    assert order is None
    assert MISSING_ORDER_ID not in SCENARIO_ORDERS


def test_regeneration_is_deterministic():
    assert build_scenario_orders() == SCENARIO_ORDERS
    assert build_scenario_tickets() == SCENARIO_TICKETS


def test_amounts_are_decimals_not_floats():
    for order in SCENARIO_ORDERS.values():
        assert isinstance(order.total_amount, Decimal)
        assert isinstance(order.refunded_amount, Decimal)
    for ticket in SCENARIO_TICKETS.values():
        if ticket.claimed_amount is not None:
            assert isinstance(ticket.claimed_amount, Decimal)


def test_unknown_scenario_raises():
    with pytest.raises(KeyError):
        get_scenario("does_not_exist")
