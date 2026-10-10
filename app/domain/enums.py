"""Domain enums for SupportFlow.

Plain ``str`` enums, free of framework dependencies, shared by the domain
models, persistence and API layers. They serialize directly to their
lowercase ``value`` in JSON.
"""

from enum import Enum


class TicketStatus(str, Enum):
    """Lifecycle of a support ticket."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    PENDING_APPROVAL = "pending_approval"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class IssueType(str, Enum):
    """What the customer is reporting."""

    REFUND_REQUEST = "refund_request"
    ORDER_INQUIRY = "order_inquiry"
    DELIVERY_ISSUE = "delivery_issue"
    DAMAGED_ITEM = "damaged_item"
    OTHER = "other"


class RiskLevel(str, Enum):
    """Risk of acting on the ticket without human review."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# Enum comparison is alphabetical, which is meaningless for risk; this
# mapping is the single source of truth for severity ordering.
RISK_SEVERITY: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


class OrderStatus(str, Enum):
    """Order lifecycle in the (simulated) commerce backend."""

    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class RefundStatus(str, Enum):
    """Lifecycle of a simulated refund."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROCESSED = "processed"
    FAILED = "failed"


class ApprovalStatus(str, Enum):
    """Lifecycle of a human approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class AgentRunStatus(str, Enum):
    """Lifecycle of one agent run."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
