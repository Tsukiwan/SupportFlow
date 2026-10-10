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
  of the same order (one unresolved refund per order: PENDING or UNKNOWN);
- the ticket-level claim cap (cumulative refunds <= ticket.claimed_amount,
  over PROCESSED/PENDING/UNKNOWN records) as a constraint/aggregate over
  the refunds table, so it also holds across processes and restarts.
"""

import threading
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from app.domain.enums import AgentRunStatus, IssueType, RefundStatus, TicketStatus
from app.domain.models import (
    AgentRun,
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
        "claim_exceeded",
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
        ticket_claimed_amount: Decimal | None,
        now: datetime,
    ) -> RefundCreateOutcome:
        """Atomic: idempotency lookup + in-flight check + balance check +
        ticket claim check + balance reserve + record insert. Returns the
        next step the service must take.

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

        Ticket-level claim identity: one ticket carries one claim, and the
        cumulative amount of all its refunds (settled, in-flight and
        unresolved) plus this request must not exceed the ticket's claimed
        amount. This check and the reservation below run inside the same
        critical section, so concurrent runs for the same ticket cannot
        both occupy the claim — a different plan id or idempotency key
        does not bypass it. A ticket without a claim amount takes no
        refunds (fail closed): the agent always validates claims before
        proposing, so a claimless ticket at this point means a caller
        bypassed the graph.

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

            # Claim cap. Only settled, in-flight and unresolved refunds
            # occupy the claim: FAILED/REJECTED refunds released their
            # reservation and leave room for a legitimate retry.
            claimed = (
                ticket_claimed_amount
                if ticket_claimed_amount is not None
                else Decimal("0")
            )
            occupied = sum(
                (
                    r.amount
                    for r in self._by_id.values()
                    if r.ticket_id == ticket_id
                    and r.status
                    in (
                        RefundStatus.PROCESSED,
                        RefundStatus.PENDING,
                        RefundStatus.UNKNOWN,
                    )
                ),
                Decimal("0"),
            )
            if amount > claimed - occupied:
                return RefundCreateOutcome(kind="claim_exceeded")

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

    def reopen(
        self,
        refund_id: str,
        *,
        orders: OrderStore,
        ticket_claimed_amount: Decimal | None,
    ) -> tuple[Literal["reopened", "balance_exceeded", "claim_exceeded"], Refund | None]:
        """Retry a transiently failed refund: atomically re-check the
        ticket claim cap, re-reserve the balance and move the record back
        to PENDING. The claim check counts only the ticket's
        PROCESSED/PENDING/UNKNOWN records — the FAILED record being
        reopened holds no reservation and is deliberately excluded, so a
        retry of the original operation must fit on top of everything else
        that currently occupies the claim. Nothing is reserved and no
        state changes unless every check passes, so a refused reopen never
        leaves a reservation behind and never reaches the gateway."""
        with self._lock:
            refund = self._by_id.get(refund_id)
            if refund is None or refund.status is not RefundStatus.FAILED:
                return "balance_exceeded", None
            claimed = (
                ticket_claimed_amount
                if ticket_claimed_amount is not None
                else Decimal("0")
            )
            occupied = sum(
                (
                    r.amount
                    for r in self._by_id.values()
                    if r.ticket_id == refund.ticket_id
                    and r.status
                    in (
                        RefundStatus.PROCESSED,
                        RefundStatus.PENDING,
                        RefundStatus.UNKNOWN,
                    )
                ),
                Decimal("0"),
            )
            if refund.amount > claimed - occupied:
                return "claim_exceeded", None
            if orders.reserve(refund.order_id, refund.amount) is None:
                return "balance_exceeded", None
            updated = refund.model_copy(update={"status": RefundStatus.PENDING})
            self._by_id[refund_id] = updated
            return "reopened", updated


class TicketStore:
    def __init__(self, tickets: dict[str, Ticket] | None = None) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, Ticket] = dict(tickets or {})

    def get(self, ticket_id: str) -> Ticket | None:
        with self._lock:
            return self._tickets.get(ticket_id)

    def update_status(
        self, ticket_id: str, status: TicketStatus, *, now: datetime
    ) -> Ticket | None:
        """Replace the ticket with a copy carrying the new status. The
        agent layer is the only caller; tools never change ticket state."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                return None
            updated = ticket.model_copy(
                update={"status": status, "updated_at": now}
            )
            self._tickets[ticket_id] = updated
            return updated

    def transition_status(
        self,
        ticket_id: str,
        status: TicketStatus,
        *,
        now: datetime,
        allowed_from: tuple[TicketStatus, ...],
    ) -> Ticket | None:
        """Atomically move the ticket to ``status`` only if its current
        status is one of ``allowed_from`` (guard checked and applied under
        the same lock). Returns None and changes nothing when the guard
        does not hold — callers use this so a concurrent run can never
        pull a terminal ticket (RESOLVED/CLOSED) backwards to an earlier
        state."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None or ticket.status not in allowed_from:
                return None
            updated = ticket.model_copy(
                update={"status": status, "updated_at": now}
            )
            self._tickets[ticket_id] = updated
            return updated


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


class AgentRunStore:
    """Bookkeeping for agent runs. ``update`` re-validates the frozen
    ``AgentRun`` model, so an iteration count or terminal status that
    violates the model constraints is rejected instead of persisted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, AgentRun] = {}

    def create(self, run: AgentRun) -> None:
        with self._lock:
            self._runs[run.run_id] = run

    def get(self, run_id: str) -> AgentRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def all(self) -> tuple[AgentRun, ...]:
        with self._lock:
            return tuple(self._runs.values())

    def update(
        self,
        run_id: str,
        *,
        status: AgentRunStatus | None = None,
        iterations: int | None = None,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> AgentRun | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            updates: dict = {}
            if status is not None:
                updates["status"] = status
            if iterations is not None:
                updates["iterations"] = iterations
            if error is not None:
                updates["error"] = error
            if finished_at is not None:
                updates["finished_at"] = finished_at
            updated = run.model_copy(update=updates)  # re-validates constraints
            self._runs[run_id] = updated
            return updated
