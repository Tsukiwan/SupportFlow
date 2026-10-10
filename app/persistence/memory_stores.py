"""In-memory stores with explicit atomic sections (Day 2 test stores).

Every store is synchronous and guarded by a ``threading.Lock``. The
critical check-then-act sequences — idempotency lookup, in-flight
detection, live balance reserve, record insert — run inside a single lock
hold without yielding, which makes them atomic with respect to the async
tool layer (tools await only around the simulated payment gateway).

These stores are an approximation for tests, not production guarantees.
On Day 4 the real guarantees must be implemented and verified in
PostgreSQL:

- ``UNIQUE(idempotency_key)`` on the refunds table (cross-process and
  cross-restart idempotency, which this in-memory store cannot provide);
- a conditional balance UPDATE over the reservation columns
  (``UPDATE orders SET reserved_amount = reserved_amount + :amount
  WHERE order_id = :id AND refunded_amount + reserved_amount + :amount
  <= total_amount``) plus row locking for reservation, release and settle;
- a unique partial index or in-flight flag to block concurrent processing
  of the same order (one unresolved refund per order: PENDING or UNKNOWN).
"""

import threading
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from app.domain.enums import IssueType, RefundStatus
from app.domain.models import (
    Approval,
    AuditEvent,
    Order,
    Refund,
    Shipment,
    Ticket,
    TicketNote,
)


class OrderStore:
    """Live order state. ``refunded_amount`` (confirmed) and
    ``reserved_amount`` (in-flight or unresolved) here are the only trusted
    balance — caller-provided balances are never used."""

    def __init__(self, orders: dict[str, Order] | None = None) -> None:
        self._lock = threading.Lock()
        self._orders: dict[str, Order] = dict(orders or {})

    def get(self, order_id: str) -> Order | None:
        with self._lock:
            return self._orders.get(order_id)

    def reserve(self, order_id: str, amount: Decimal) -> Order | None:
        """Atomically check the live refundable balance and reserve it."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None or amount > order.refundable_amount:
                return None
            updated = order.model_copy(
                update={"reserved_amount": order.reserved_amount + amount}
            )
            self._orders[order_id] = updated
            return updated

    def release(self, order_id: str, amount: Decimal) -> None:
        """Release a reservation (FAILED/REJECTED); never drops it below
        zero and never touches the confirmed refunded amount."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return
            updated = order.model_copy(
                update={
                    "reserved_amount": max(
                        Decimal("0"), order.reserved_amount - amount
                    )
                }
            )
            self._orders[order_id] = updated

    def settle(self, order_id: str, amount: Decimal) -> None:
        """Move a reservation to the confirmed refunded amount once the
        gateway result is PROCESSED."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return
            updated = order.model_copy(
                update={
                    "reserved_amount": max(
                        Decimal("0"), order.reserved_amount - amount
                    ),
                    "refunded_amount": order.refunded_amount + amount,
                }
            )
            self._orders[order_id] = updated


class RefundCreateOutcome(BaseModel):
    """Result of the atomic create step inside :class:`RefundStore`."""

    kind: Literal[
        "execute",
        "already_processed",
        "retry",
        "needs_resolution",
        "concurrent",
        "unresolved",
        "conflict",
        "balance_exceeded",
        "already_refunded",
        "gateway_rejected",
    ]
    refund: Refund | None = None


class RefundStore:
    """Refund records plus the atomic idempotency / reservation section."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, Refund] = {}
        self._by_key: dict[str, str] = {}

    def count(self) -> int:
        with self._lock:
            return len(self._by_id)

    def get(self, refund_id: str) -> Refund | None:
        with self._lock:
            return self._by_id.get(refund_id)

    def get_by_key(self, idempotency_key: str) -> Refund | None:
        with self._lock:
            refund_id = self._by_key.get(idempotency_key)
            return self._by_id.get(refund_id) if refund_id else None

    def all(self) -> tuple[Refund, ...]:
        with self._lock:
            return tuple(self._by_id.values())

    def create(
        self,
        orders: OrderStore,
        *,
        order_id: str,
        amount: Decimal,
        idempotency_key: str,
        ticket_id: str,
        issue_type: IssueType,
        plan_id: str | None,
        run_id: str | None,
        reason: str,
        now: datetime,
    ) -> RefundCreateOutcome:
        """Atomic: idempotency lookup + in-flight check + balance reserve
        + record insert. Returns the next step the service must take.

        The idempotency key binds the full operation intent (order, amount,
        ticket, issue type and plan version) — a same-key call with any
        intent field changed conflicts instead of silently replaying.
        ``run_id`` is traceability metadata and is deliberately excluded
        from the intent comparison: a retry from a different run with the
        same key must resolve the original record, not conflict.

        An order may have at most one unresolved refund: a PENDING refund
        (another request is executing right now) blocks new refunds as
        concurrent, and an UNKNOWN refund (the original operation must be
        resolved first) blocks them as unresolved. Same-key retries are
        matched before this scan and keep their status-query recovery path.

        The refund record is fully constructed and validated before any
        balance is reserved, so a construction failure cannot leak a
        reservation.
        """
        with self._lock:
            existing_id = self._by_key.get(idempotency_key)
            if existing_id is not None:
                existing = self._by_id[existing_id]
                if (
                    existing.order_id != order_id
                    or existing.amount != amount
                    or existing.ticket_id != ticket_id
                    or existing.issue_type is not issue_type
                    or existing.plan_id != plan_id
                ):
                    return RefundCreateOutcome(kind="conflict")
                if existing.status is RefundStatus.PROCESSED:
                    return RefundCreateOutcome(
                        kind="already_processed", refund=existing
                    )
                if existing.status is RefundStatus.REJECTED:
                    return RefundCreateOutcome(
                        kind="gateway_rejected", refund=existing
                    )
                if existing.status is RefundStatus.UNKNOWN:
                    return RefundCreateOutcome(
                        kind="needs_resolution", refund=existing
                    )
                if existing.status is RefundStatus.FAILED:
                    return RefundCreateOutcome(kind="retry", refund=existing)
                return RefundCreateOutcome(kind="concurrent", refund=existing)

            # A different idempotency key may still target the same order,
            # but an order with an unresolved refund takes no new refunds
            # until the original operation is settled.
            for refund in self._by_id.values():
                if refund.order_id != order_id:
                    continue
                if refund.status is RefundStatus.PENDING:
                    return RefundCreateOutcome(kind="concurrent")
                if refund.status is RefundStatus.UNKNOWN:
                    return RefundCreateOutcome(kind="unresolved", refund=refund)

            order = orders.get(order_id)
            if order is None or amount > order.refundable_amount:
                if order is not None and order.refunded_amount >= order.total_amount:
                    return RefundCreateOutcome(kind="already_refunded")
                return RefundCreateOutcome(kind="balance_exceeded")

            # Construct and validate the record first: if this raises, no
            # reservation has been made yet.
            refund = Refund(
                refund_id=f"REF-{uuid4().hex[:12]}",
                idempotency_key=idempotency_key,
                ticket_id=ticket_id,
                order_id=order_id,
                amount=amount,
                currency=order.currency,
                issue_type=issue_type,
                plan_id=plan_id,
                run_id=run_id,
                status=RefundStatus.PENDING,
                reason=reason,
                created_at=now,
            )

            if orders.reserve(order_id, amount) is None:
                return RefundCreateOutcome(kind="balance_exceeded")

            self._by_id[refund.refund_id] = refund
            self._by_key[idempotency_key] = refund.refund_id
            return RefundCreateOutcome(kind="execute", refund=refund)

    def finalize(
        self,
        refund_id: str,
        status: RefundStatus,
        *,
        orders: OrderStore,
        now: datetime,
    ) -> Refund | None:
        """Move a PENDING/UNKNOWN refund to a terminal status. The balance
        reservation is released on definitive failure (FAILED/REJECTED),
        settled into the confirmed refunded amount on PROCESSED, and kept
        for UNKNOWN — fail closed."""
        with self._lock:
            refund = self._by_id.get(refund_id)
            if refund is None or refund.status not in (
                RefundStatus.PENDING,
                RefundStatus.UNKNOWN,
            ):
                return None
            if status in (RefundStatus.FAILED, RefundStatus.REJECTED):
                orders.release(refund.order_id, refund.amount)
            elif status is RefundStatus.PROCESSED:
                orders.settle(refund.order_id, refund.amount)
            updates: dict = {"status": status}
            if status is RefundStatus.PROCESSED:
                updates["processed_at"] = now
            updated = refund.model_copy(update=updates)
            self._by_id[refund_id] = updated
            return updated

    def reopen(self, refund_id: str, *, orders: OrderStore) -> Refund | None:
        """Retry a transiently failed refund: atomically re-reserve the
        balance and move the record back to PENDING."""
        with self._lock:
            refund = self._by_id.get(refund_id)
            if refund is None or refund.status is not RefundStatus.FAILED:
                return None
            if orders.reserve(refund.order_id, refund.amount) is None:
                return None
            updated = refund.model_copy(update={"status": RefundStatus.PENDING})
            self._by_id[refund_id] = updated
            return updated


class TicketStore:
    def __init__(self, tickets: dict[str, Ticket] | None = None) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, Ticket] = dict(tickets or {})

    def get(self, ticket_id: str) -> Ticket | None:
        with self._lock:
            return self._tickets.get(ticket_id)


class TicketNoteStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._notes: dict[str, list[TicketNote]] = {}

    def add(self, note: TicketNote) -> None:
        with self._lock:
            self._notes.setdefault(note.ticket_id, []).append(note)

    def list_for(self, ticket_id: str) -> tuple[TicketNote, ...]:
        with self._lock:
            return tuple(self._notes.get(ticket_id, ()))


class ShippingStore:
    def __init__(self, shipments: dict[str, Shipment] | None = None) -> None:
        self._lock = threading.Lock()
        self._shipments: dict[str, Shipment] = dict(shipments or {})

    def get(self, order_id: str) -> Shipment | None:
        with self._lock:
            return self._shipments.get(order_id)


class ApprovalStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._approvals: dict[str, Approval] = {}

    def add(self, approval: Approval) -> None:
        with self._lock:
            self._approvals[approval.approval_id] = approval

    def get(self, approval_id: str) -> Approval | None:
        with self._lock:
            return self._approvals.get(approval_id)

    def all(self) -> tuple[Approval, ...]:
        with self._lock:
            return tuple(self._approvals.values())


class AuditStore:
    """Append-only audit trail for the lifetime of a process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[AuditEvent] = []

    def add(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def all(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._events)
