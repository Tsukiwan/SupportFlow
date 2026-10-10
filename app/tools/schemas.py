"""Typed input models for the tool layer.

Each tool builds its input model first; pydantic validation errors are
converted to ``ToolResult(INVALID_INPUT)`` by the audit wrapper, so a tool
never raises into agent logic. ``CreateRefundInput.amount`` reuses the
domain money rules: Decimal only, > 0, at most 2 decimal places.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import IssueType, OrderStatus
from app.domain.money import PositiveMoney


class GetOrderInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str = Field(min_length=1, max_length=64)


class GetShippingStatusInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str = Field(min_length=1, max_length=64)


class GetRefundPolicyInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    issue_type: IssueType
    order_status: OrderStatus


class CreateRefundInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str = Field(min_length=1, max_length=64)
    amount: PositiveMoney
    issue_type: IssueType
    idempotency_key: str = Field(min_length=8, max_length=128)
    # Plan version the agent is executing against; mandatory whenever the
    # policy requires approval, and must equal the approval's plan_id.
    plan_id: str | None = Field(default=None, min_length=1, max_length=64)


class AddTicketNoteInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticket_id: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=2000)
