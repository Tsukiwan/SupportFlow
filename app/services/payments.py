"""Simulated payment gateway.

Day 2 simulates refunds end to end without touching a real payment
platform. The gateway is the single integration point that can produce
temporary failures, rejections or unknown outcomes; everything else in
the refund pipeline is deterministic.

``SimulatedPaymentGateway`` yields once per call (``asyncio.sleep(0)``) so
concurrent requests can interleave, which exercises the in-flight
protections of the refund store in tests.
"""

import asyncio
from enum import Enum
from typing import Protocol

from app.domain.models import Refund


class GatewayOutcome(str, Enum):
    PROCESSED = "processed"
    REJECTED = "rejected"
    TRANSIENT_FAILURE = "transient_failure"
    UNKNOWN = "unknown"


class PaymentGateway(Protocol):
    async def execute(self, refund: Refund) -> GatewayOutcome: ...

    async def status(self, refund_id: str) -> GatewayOutcome: ...


class SimulatedPaymentGateway:
    """Pops one outcome per call from a queue; defaults to PROCESSED.

    ``execute_calls`` / ``status_calls`` count gateway invocations so tests
    can prove a recovery path queries the original operation instead of
    blindly executing a second refund."""

    def __init__(self, outcomes: list[GatewayOutcome] | None = None) -> None:
        self._queue: list[GatewayOutcome] = list(outcomes or [])
        self.calls = 0
        self.execute_calls = 0
        self.status_calls = 0

    def _next(self) -> GatewayOutcome:
        self.calls += 1
        if self._queue:
            return self._queue.pop(0)
        return GatewayOutcome.PROCESSED

    async def execute(self, refund: Refund) -> GatewayOutcome:
        self.execute_calls += 1
        await asyncio.sleep(0)
        return self._next()

    async def status(self, refund_id: str) -> GatewayOutcome:
        self.status_calls += 1
        await asyncio.sleep(0)
        return self._next()
