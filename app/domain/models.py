"""Core domain models for SupportFlow.

Plain Pydantic models — no LangGraph, no ORM, no framework imports — so the
agent, services, API and persistence layers can all consume them unchanged.

All timestamps are timezone-aware UTC (enforced via ``AwareDatetime``) and
all money is ``Decimal`` (see :mod:`app.domain.money`).
"""

from decimal import Decimal
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

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
from app.domain.money import Money, PositiveMoney

CURRENCY_PATTERN = r"^[A-Z]{3}$"


class Order(BaseModel):
    """A customer order from the (simulated) commerce backend."""

    model_config = ConfigDict(frozen=True)

    order_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    status: OrderStatus
    currency: str = Field(pattern=CURRENCY_PATTERN)
    total_amount: Money
    # Only refunds whose gateway result is confirmed count as refunded;
    # in-flight or unresolved refunds hold a separate reservation instead.
    refunded_amount: Money = Decimal("0")
    reserved_amount: Money = Decimal("0")
    created_at: AwareDatetime
    delivered_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _validate_timeline(self) -> "Order":
        if self.delivered_at is not None and self.delivered_at < self.created_at:
            raise ValueError("delivered_at must not precede created_at")
        return self

    @property
    def refundable_amount(self) -> Decimal:
        """Amount neither refunded nor reserved by an in-flight refund."""
        return self.total_amount - self.refunded_amount - self.reserved_amount


class Ticket(BaseModel):
    """A customer-support ticket referencing an order."""

    model_config = ConfigDict(frozen=True)

    ticket_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    status: TicketStatus = TicketStatus.OPEN
    issue_type: IssueType
    risk_level: RiskLevel = RiskLevel.LOW
    subject: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10)
    claimed_amount: PositiveMoney | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _validate(self) -> "Ticket":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.issue_type is IssueType.REFUND_REQUEST and self.claimed_amount is None:
            raise ValueError("refund_request tickets must state a claimed_amount")
        return self


class Refund(BaseModel):
    """A (simulated) refund. Never wired to a real payment gateway."""

    model_config = ConfigDict(frozen=True)

    refund_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    ticket_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    amount: PositiveMoney
    currency: str = Field(pattern=CURRENCY_PATTERN)
    # The issue type comes from the ticket record (never from caller
    # claims) and is part of the idempotency intent bound by the store.
    issue_type: IssueType
    plan_id: str | None = Field(default=None, min_length=1, max_length=64)
    # Which agent run created this refund. Traceability metadata only —
    # it is deliberately NOT part of the idempotency intent, so a retry
    # from a different run with the same key resolves the original record.
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    status: RefundStatus = RefundStatus.PENDING
    reason: str = Field(min_length=10)
    created_at: AwareDatetime
    processed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> "Refund":
        if self.processed_at is not None and self.processed_at < self.created_at:
            raise ValueError("processed_at must not precede created_at")
        return self


class Approval(BaseModel):
    """Human approval gate for high-risk actions (e.g. refunds)."""

    model_config = ConfigDict(frozen=True)

    approval_id: str = Field(min_length=1, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=64)
    # A refund approval is only valid for the exact order, amount and plan
    # version it was granted for — checked by RefundService, not by the LLM.
    order_id: str = Field(min_length=1, max_length=64)
    plan_id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=64)
    amount: PositiveMoney | None = None
    risk_level: RiskLevel
    justification: str = Field(min_length=10)
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: AwareDatetime
    decided_at: AwareDatetime | None = None
    decided_by: str | None = None
    # None means the approval never expires; an explicit validity window is
    # enforced by RefundService before the approval unlocks a refund.
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> "Approval":
        if self.decided_at is not None and self.decided_at < self.requested_at:
            raise ValueError("decided_at must not precede requested_at")
        if self.expires_at is not None and self.expires_at < self.requested_at:
            raise ValueError("expires_at must not precede requested_at")
        decided = self.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)
        if decided and (self.decided_at is None or self.decided_by is None):
            raise ValueError("decided approvals require decided_at and decided_by")
        if not decided and (self.decided_at is not None or self.decided_by is not None):
            raise ValueError("undecided approvals must not carry decision fields")
        return self


class Shipment(BaseModel):
    """Carrier tracking record for one order (simulated)."""

    model_config = ConfigDict(frozen=True)

    order_id: str = Field(min_length=1, max_length=64)
    carrier: str = Field(min_length=1, max_length=64)
    tracking_number: str = Field(min_length=1, max_length=64)
    status: ShippingStatus
    status_updated_at: AwareDatetime
    evidence: tuple[str, ...] = Field(default_factory=tuple)


class TicketNote(BaseModel):
    """Free-text note appended to a ticket (e.g. by the agent)."""

    model_config = ConfigDict(frozen=True)

    note_id: str = Field(min_length=1, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=64)
    author: Literal["agent", "human"] = "agent"
    text: str = Field(min_length=1, max_length=2000)
    created_at: AwareDatetime


class AgentRun(BaseModel):
    """Bookkeeping for one agent run. Iterations are hard-capped."""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=64)
    status: AgentRunStatus = AgentRunStatus.RUNNING
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    iterations: int = Field(default=0, ge=0)
    max_iterations: int = Field(default=10, gt=0)
    error: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> "AgentRun":
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.iterations > self.max_iterations:
            raise ValueError("iterations must not exceed max_iterations")
        terminal = self.status in (
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        )
        if terminal and self.finished_at is None:
            raise ValueError("terminal runs require finished_at")
        if self.status is not AgentRunStatus.FAILED and self.error is not None:
            raise ValueError("only failed runs may carry an error")
        return self


_SECRET_KEY_PATTERNS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
)


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_").replace(" ", "_")
    return any(pattern in normalized for pattern in _SECRET_KEY_PATTERNS)


class AuditEvent(BaseModel):
    """Append-only audit trail entry. Must never carry secrets."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=64)
    timestamp: AwareDatetime
    actor: Literal["system", "agent", "human"]
    action: str = Field(min_length=1, max_length=128)
    ticket_id: str | None = None
    order_id: str | None = None
    # Which agent run the event belongs to (traceability, not authorization).
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_no_secrets(self) -> "AuditEvent":
        for key in self.details:
            if _is_secret_key(key):
                raise ValueError(f"audit details must not contain secret-like key {key!r}")
        return self


class ResolutionPlanStep(BaseModel):
    """A single step of a proposed resolution, with its rationale."""

    model_config = ConfigDict(frozen=True)

    action: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


class ResolutionPlan(BaseModel):
    """Proposed handling of a ticket. High-risk plans require approval."""

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=64)
    risk_level: RiskLevel
    requires_approval: bool
    steps: tuple[ResolutionPlanStep, ...] = Field(min_length=1)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_approval_rule(self) -> "ResolutionPlan":
        high_risk = RISK_SEVERITY[self.risk_level] >= RISK_SEVERITY[RiskLevel.HIGH]
        if high_risk and not self.requires_approval:
            raise ValueError("plans with HIGH or CRITICAL risk must require approval")
        return self
