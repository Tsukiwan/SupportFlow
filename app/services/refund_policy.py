"""Deterministic refund policy.

Pure functions over ``(issue_type, order_status)`` — no LLM, no I/O, no
store access — so the safety rules are testable in isolation and cannot be
influenced by agent claims. Anything not explicitly listed in the table
fails closed (``allowed=False``).
"""

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import IssueType, OrderStatus, RiskLevel
from app.domain.money import Money


class RefundPolicy(BaseModel):
    """Structured answer of the refund policy engine."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str = Field(min_length=1)
    requires_approval: bool
    approval_risk_level: RiskLevel = RiskLevel.LOW
    # None means "up to the live refundable balance of the order".
    max_amount: Money | None = None


# (allowed, reason, requires_approval, approval_risk_level)
_POLICY_TABLE: dict[tuple[IssueType, OrderStatus], tuple[bool, str, bool, RiskLevel]] = {
    (IssueType.REFUND_REQUEST, OrderStatus.DELIVERED): (
        True,
        "Delivered orders are refundable within the return window.",
        False,
        RiskLevel.LOW,
    ),
    (IssueType.REFUND_REQUEST, OrderStatus.CANCELLED): (
        True,
        "Cancelled orders are refunded in full.",
        False,
        RiskLevel.LOW,
    ),
    (IssueType.REFUND_REQUEST, OrderStatus.SHIPPED): (
        False,
        "Shipped orders must be returned or cancelled before a refund.",
        False,
        RiskLevel.LOW,
    ),
    (IssueType.REFUND_REQUEST, OrderStatus.PAID): (
        False,
        "Orders that are paid but not shipped are not refundable via support tickets.",
        False,
        RiskLevel.LOW,
    ),
    (IssueType.REFUND_REQUEST, OrderStatus.PENDING): (
        False,
        "Orders that are not yet paid are not refundable.",
        False,
        RiskLevel.LOW,
    ),
    (IssueType.DELIVERY_ISSUE, OrderStatus.DELIVERED): (
        True,
        "Delivered-but-not-received claims require human review.",
        True,
        RiskLevel.HIGH,
    ),
    (IssueType.DELIVERY_ISSUE, OrderStatus.SHIPPED): (
        True,
        "Lost-in-transit claims require human review.",
        True,
        RiskLevel.HIGH,
    ),
    (IssueType.DELIVERY_ISSUE, OrderStatus.CANCELLED): (
        False,
        "No delivery issues apply to cancelled orders.",
        False,
        RiskLevel.LOW,
    ),
    (IssueType.DELIVERY_ISSUE, OrderStatus.PAID): (
        False,
        "No delivery issues apply before shipment.",
        False,
        RiskLevel.LOW,
    ),
    (IssueType.DELIVERY_ISSUE, OrderStatus.PENDING): (
        False,
        "No delivery issues apply before shipment.",
        False,
        RiskLevel.LOW,
    ),
    (IssueType.DAMAGED_ITEM, OrderStatus.DELIVERED): (
        True,
        "Damaged-item refunds require human review.",
        True,
        RiskLevel.HIGH,
    ),
    (IssueType.DAMAGED_ITEM, OrderStatus.SHIPPED): (
        True,
        "In-transit damage claims require human review.",
        True,
        RiskLevel.HIGH,
    ),
    (IssueType.DAMAGED_ITEM, OrderStatus.CANCELLED): (
        False,
        "No damage claims apply to cancelled orders.",
        False,
        RiskLevel.LOW,
    ),
    (IssueType.ORDER_INQUIRY, OrderStatus.DELIVERED): (
        False,
        "Order inquiries are not refund requests.",
        False,
        RiskLevel.LOW,
    ),
    (IssueType.ORDER_INQUIRY, OrderStatus.CANCELLED): (
        False,
        "Order inquiries are not refund requests.",
        False,
        RiskLevel.LOW,
    ),
    (IssueType.OTHER, OrderStatus.DELIVERED): (
        False,
        "No refund policy matches this issue type.",
        False,
        RiskLevel.LOW,
    ),
}


def evaluate_refund_policy(
    issue_type: IssueType, order_status: OrderStatus
) -> RefundPolicy:
    rule = _POLICY_TABLE.get((issue_type, order_status))
    if rule is None:
        # Fail closed: any combination not explicitly listed is denied.
        return RefundPolicy(
            allowed=False,
            reason="No refund policy matches this combination.",
            requires_approval=False,
            approval_risk_level=RiskLevel.LOW,
        )
    allowed, reason, requires_approval, risk_level = rule
    return RefundPolicy(
        allowed=allowed,
        reason=reason,
        requires_approval=requires_approval,
        approval_risk_level=risk_level,
    )
