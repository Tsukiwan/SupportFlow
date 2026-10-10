"""Structured refund policy: deterministic, LLM-free, fail-closed."""

import pytest

from app.domain.enums import IssueType, OrderStatus, RiskLevel
from app.services.refund_policy import evaluate_refund_policy


def test_refund_request_on_delivered_order_is_allowed():
    policy = evaluate_refund_policy(IssueType.REFUND_REQUEST, OrderStatus.DELIVERED)
    assert policy.allowed is True
    assert policy.requires_approval is False
    assert policy.max_amount is None  # up to the live refundable balance


def test_refund_request_on_cancelled_order_is_allowed():
    policy = evaluate_refund_policy(IssueType.REFUND_REQUEST, OrderStatus.CANCELLED)
    assert policy.allowed is True
    assert policy.requires_approval is False


def test_delivery_issue_on_delivered_order_requires_approval():
    policy = evaluate_refund_policy(IssueType.DELIVERY_ISSUE, OrderStatus.DELIVERED)
    assert policy.allowed is True
    assert policy.requires_approval is True
    assert policy.approval_risk_level is RiskLevel.HIGH


def test_delivery_issue_on_shipped_order_requires_approval():
    policy = evaluate_refund_policy(IssueType.DELIVERY_ISSUE, OrderStatus.SHIPPED)
    assert policy.allowed is True
    assert policy.requires_approval is True


def test_damaged_item_on_delivered_order_requires_approval():
    policy = evaluate_refund_policy(IssueType.DAMAGED_ITEM, OrderStatus.DELIVERED)
    assert policy.allowed is True
    assert policy.requires_approval is True


@pytest.mark.parametrize(
    ("issue_type", "order_status"),
    [
        (IssueType.REFUND_REQUEST, OrderStatus.SHIPPED),
        (IssueType.REFUND_REQUEST, OrderStatus.PAID),
        (IssueType.REFUND_REQUEST, OrderStatus.PENDING),
        (IssueType.DELIVERY_ISSUE, OrderStatus.CANCELLED),
        (IssueType.DELIVERY_ISSUE, OrderStatus.PAID),
        (IssueType.ORDER_INQUIRY, OrderStatus.DELIVERED),
        (IssueType.ORDER_INQUIRY, OrderStatus.CANCELLED),
        (IssueType.OTHER, OrderStatus.DELIVERED),
    ],
)
def test_disallowed_combinations_are_denied(issue_type, order_status):
    policy = evaluate_refund_policy(issue_type, order_status)
    assert policy.allowed is False
    assert len(policy.reason) > 0


def test_unlisted_combination_fails_closed():
    # DAMAGED_ITEM + CANCELLED is not in the table: default is deny.
    policy = evaluate_refund_policy(IssueType.DAMAGED_ITEM, OrderStatus.CANCELLED)
    assert policy.allowed is False


def test_policy_is_structured_not_boolean_only():
    policy = evaluate_refund_policy(IssueType.REFUND_REQUEST, OrderStatus.DELIVERED)
    assert isinstance(policy.reason, str)
    assert policy.requires_approval is False
    assert policy.approval_risk_level is RiskLevel.LOW
