"""Query tools: get_order, get_shipping_status, get_refund_policy."""

import asyncio
from decimal import Decimal

from app.domain.enums import IssueType, OrderStatus, ShippingStatus
from app.tools.result import ToolErrorCode
from tests.tools_util import build_tool_set, ctx


def run(coro):
    return asyncio.run(coro)


def test_get_order_returns_domain_order():
    ts = build_tool_set()
    result = run(ts.get_order(ctx(), order_id="ORD-1001"))
    assert result.success is True
    assert result.data.order_id == "ORD-1001"
    assert result.data.status is OrderStatus.DELIVERED
    assert result.data.total_amount == Decimal("199.99")  # Decimal-safe


def test_get_order_not_found():
    ts = build_tool_set()
    result = run(ts.get_order(ctx(), order_id="ORD-9999"))
    assert result.success is False
    assert result.error_code is ToolErrorCode.ORDER_NOT_FOUND
    assert result.data is None


def test_get_shipping_status_delivered_with_evidence():
    ts = build_tool_set()
    result = run(ts.get_shipping_status(ctx(), order_id="ORD-1001"))
    assert result.success is True
    assert result.data.order_id == "ORD-1001"
    assert result.data.status is ShippingStatus.DELIVERED
    assert "delivery_photo" in result.data.evidence


def test_get_shipping_status_disputed_delivery_has_evidence():
    ts = build_tool_set()
    result = run(ts.get_shipping_status(ctx(), order_id="ORD-1005"))
    assert result.success is True
    assert "signature_confirmed" in result.data.evidence


def test_get_shipping_status_missing_record():
    ts = build_tool_set()
    result = run(ts.get_shipping_status(ctx(), order_id="ORD-1002"))
    assert result.success is False
    assert result.error_code is ToolErrorCode.SHIPPING_NOT_FOUND


def test_get_shipping_status_unknown_order():
    ts = build_tool_set()
    result = run(ts.get_shipping_status(ctx(), order_id="ORD-9999"))
    assert result.error_code is ToolErrorCode.ORDER_NOT_FOUND


def test_get_refund_policy_tool_returns_structured_policy():
    ts = build_tool_set()
    result = run(
        ts.get_refund_policy(
            ctx(),
            issue_type=IssueType.REFUND_REQUEST,
            order_status=OrderStatus.DELIVERED,
        )
    )
    assert result.success is True
    assert result.data.allowed is True
    assert result.data.requires_approval is False
