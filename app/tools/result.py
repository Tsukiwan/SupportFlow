"""Uniform tool-call result envelope.

Tools report outcomes through :class:`ToolResult` instead of raising
business exceptions into agent logic, so callers branch on ``error_code``
and ``retryable`` without parsing exception types. Failures never smuggle
exceptions into ``data``.
"""

from __future__ import annotations

from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")


class ToolErrorCode(str, Enum):
    """Canonical error codes shared by all tools."""

    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ALREADY_REFUNDED = "ALREADY_REFUNDED"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    DELIVERY_CONFIRMED = "DELIVERY_CONFIRMED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    INVALID_INPUT = "INVALID_INPUT"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # Day 2: refund safety boundary
    POLICY_DENIED = "POLICY_DENIED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    CONCURRENT_PROCESSING = "CONCURRENT_PROCESSING"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_MISMATCH = "APPROVAL_MISMATCH"
    GATEWAY_REJECTED = "GATEWAY_REJECTED"
    SHIPPING_NOT_FOUND = "SHIPPING_NOT_FOUND"
    TICKET_NOT_FOUND = "TICKET_NOT_FOUND"
    TICKET_ORDER_MISMATCH = "TICKET_ORDER_MISMATCH"
    ISSUE_TYPE_MISMATCH = "ISSUE_TYPE_MISMATCH"
    UNRESOLVED_REFUND = "UNRESOLVED_REFUND"


class ToolResult(BaseModel, Generic[T]):
    """Result of one tool call: success flag, machine-readable error code,
    human-readable message, optional payload and retry hint."""

    model_config = ConfigDict(frozen=True)

    success: bool
    error_code: ToolErrorCode | None = None
    message: str = Field(min_length=1)
    data: T | None = None
    retryable: bool = False

    @model_validator(mode="after")
    def _validate(self) -> "ToolResult[T]":
        if self.success and self.error_code is not None:
            raise ValueError("successful results must not carry an error_code")
        if not self.success and self.error_code is None:
            raise ValueError("failed results must carry an error_code")
        if self.retryable and self.success:
            raise ValueError("retryable is only meaningful for failed results")
        return self

    @classmethod
    def ok(cls, message: str, data: T | None = None) -> "ToolResult[T]":
        return cls(success=True, message=message, data=data)

    @classmethod
    def error(
        cls,
        code: ToolErrorCode,
        message: str,
        *,
        retryable: bool = False,
        data: T | None = None,
    ) -> "ToolResult[T]":
        return cls(
            success=False,
            error_code=code,
            message=message,
            retryable=retryable,
            data=data,
        )
