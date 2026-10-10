"""ToolResult envelope: uniform success/failure shape for tool calls."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.tools.result import ToolErrorCode, ToolResult


def test_ok_result_carries_data():
    result = ToolResult.ok(message="order found", data={"order_id": "ORD-1"})
    assert result.success is True
    assert result.error_code is None
    assert result.retryable is False
    assert result.data == {"order_id": "ORD-1"}


def test_error_result_carries_code():
    result = ToolResult.error(
        ToolErrorCode.ORDER_NOT_FOUND,
        "order ORD-9999 does not exist",
    )
    assert result.success is False
    assert result.error_code is ToolErrorCode.ORDER_NOT_FOUND
    assert result.retryable is False
    assert result.data is None


def test_retryable_error_for_timeouts():
    result = ToolResult.error(
        ToolErrorCode.UPSTREAM_TIMEOUT,
        "backend timed out",
        retryable=True,
    )
    assert result.retryable is True


def test_failed_result_requires_error_code():
    with pytest.raises(ValidationError):
        ToolResult(success=False, message="something went wrong")


def test_successful_result_must_not_carry_error_code():
    with pytest.raises(ValidationError):
        ToolResult(
            success=True,
            message="ok",
            error_code=ToolErrorCode.INTERNAL_ERROR,
        )


def test_retryable_only_for_failures():
    with pytest.raises(ValidationError):
        ToolResult(success=True, message="ok", retryable=True)


def test_typed_data_roundtrip():
    result: ToolResult[Decimal] = ToolResult.ok("amount", data=Decimal("1.23"))
    assert result.data == Decimal("1.23")
