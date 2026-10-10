"""Audit wrapper for tool methods.

Every tool call leaves an :class:`~app.domain.models.AuditEvent` with the
tool name, outcome category (success / invalid_input / business_rejection
/ transient_failure / unknown / execution_failure / cancelled), duration
and related ids. Exceptions are logged in full via ``logger.exception`` but
never leak into tool results — and a failed tool never resolves a ticket
on its own.

A cancelled call records ``cancelled`` — never a stale ``success`` — and
the cancellation is re-raised so the caller sees it. Audit recording itself
is shielded so a cancellation cannot silently drop the audit event.
"""

import asyncio
import functools
import logging
import time
from typing import Any

from pydantic import ValidationError

from app.domain.models import Order, Refund, Shipment, TicketNote
from app.tools.result import ToolErrorCode, ToolResult

logger = logging.getLogger(__name__)


def _classify(result: ToolResult[Any]) -> tuple[str, str]:
    code = result.error_code
    assert code is not None
    if code is ToolErrorCode.UNKNOWN_OUTCOME:
        return "unknown", code.value
    if code is ToolErrorCode.INTERNAL_ERROR:
        return "execution_failure", code.value
    if code is ToolErrorCode.INVALID_INPUT:
        return "invalid_input", code.value
    if result.retryable:
        return "transient_failure", code.value
    return "business_rejection", code.value


def _related_ids(data: Any) -> dict[str, str]:
    if isinstance(data, Refund):
        return {"refund_id": data.refund_id, "order_id": data.order_id}
    if isinstance(data, Order):
        return {"order_id": data.order_id}
    if isinstance(data, Shipment):
        return {"order_id": data.order_id}
    if isinstance(data, TicketNote):
        return {"ticket_id": data.ticket_id}
    return {}


def audited(tool_name: str):
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(self, ctx=None, **kwargs):
            started = time.perf_counter()
            outcome = "success"
            error_code: str | None = None
            related: dict[str, str] = {}
            try:
                try:
                    result = await fn(self, ctx, **kwargs)
                except asyncio.CancelledError:
                    # A cancelled call is not a successful one; record it as
                    # cancelled, then re-raise so cancellation propagates.
                    outcome = "cancelled"
                    raise
                except ValidationError as exc:
                    result = ToolResult.error(
                        ToolErrorCode.INVALID_INPUT,
                        f"invalid input: {exc}",
                        retryable=False,
                    )
                except Exception:
                    logger.exception(
                        "tool %s raised an unexpected exception", tool_name
                    )
                    result = ToolResult.error(
                        ToolErrorCode.INTERNAL_ERROR,
                        "internal tool failure",
                        retryable=False,
                    )
                if not result.success:
                    outcome, error_code = _classify(result)
                related = _related_ids(result.data)
                return result
            finally:
                try:
                    await asyncio.shield(
                        self._recorder.record(
                            tool=tool_name,
                            ctx=ctx,
                            outcome=outcome,
                            duration_ms=(time.perf_counter() - started) * 1000,
                            error_code=error_code,
                            details=related,
                        )
                    )
                except Exception:
                    logger.exception(
                        "failed to record audit event for tool %s", tool_name
                    )

        return wrapper

    return decorate
