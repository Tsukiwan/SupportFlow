"""Domain layer: enums, money rules and Pydantic models.

No framework dependencies (LangGraph, FastAPI, SQLAlchemy) here.
"""

from app.domain.enums import (
    AgentRunStatus,
    ApprovalStatus,
    IssueType,
    OrderStatus,
    RefundStatus,
    RiskLevel,
    RISK_SEVERITY,
    ShippingStatus,
    TicketStatus,
)
from app.domain.models import (
    AgentRun,
    Approval,
    AuditEvent,
    Order,
    Refund,
    ResolutionPlan,
    ResolutionPlanStep,
    Shipment,
    Ticket,
    TicketNote,
)

__all__ = [
    "AgentRun",
    "AgentRunStatus",
    "Approval",
    "ApprovalStatus",
    "AuditEvent",
    "IssueType",
    "Order",
    "OrderStatus",
    "Refund",
    "RefundStatus",
    "ResolutionPlan",
    "ResolutionPlanStep",
    "RiskLevel",
    "RISK_SEVERITY",
    "Shipment",
    "ShippingStatus",
    "Ticket",
    "TicketNote",
    "TicketStatus",
]
