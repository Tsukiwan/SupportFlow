"""PostgreSQL refund store: the refund invariants on the database.

Implements the same contract as the in-memory
:class:`~app.persistence.memory_stores.RefundStore` (see
:class:`~app.persistence.refund_store.RefundStoreProtocol`), with every
invariant enforced by PostgreSQL instead of a process-local lock:

- ``UNIQUE(idempotency_key)`` and the partial unique index
  ``uq_refunds_order_unresolved`` back the idempotency and same-order
  gates across processes and restarts;
- the balance reservation is one conditional
  ``UPDATE ... WHERE refunded + reserved + amount <= total`` on a
  locked order row — never a read-then-write;
- the claim cap is an aggregate over the refunds table computed while
  the ticket row is locked (``FOR UPDATE``), so concurrent creators for
  the same ticket serialize;
- the refund row and its audit event commit in one transaction; a rollback
  removes both, so the audit trail never shows a state the database does
  not have.

Transaction design:

- one short transaction per store operation, owned by this store via
  ``session_scope`` — no session is held across the gateway call (the
  service layer calls ``create``, commits, then calls the gateway, then
  calls ``finalize`` in a fresh transaction);
- lock order is ``orders -> tickets -> refunds`` on every path that
  takes more than one lock (the same-key fast path takes none);
- unique-constraint races are identified by PostgreSQL's constraint
  *name* (``diag.constraint_name``), mapped to the existing business
  kinds, and re-checked in a fresh transaction — every other database
  error (FK, CHECK, serialization) propagates unchanged, it is never
  disguised as a business rejection;
- READ COMMITTED; no transaction ever waits on a gateway or the network.

The store is synchronous (psycopg3 sync driver, SQLAlchemy 2.x), matching
the synchronous in-memory stores. It returns domain models, never ORM
rows.

The ``orders`` argument exists for protocol parity with the in-memory
store and is ignored: order state lives in the ``orders`` table and is
locked/updated inside the transaction.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.domain.enums import IssueType, RefundStatus
from app.domain.models import Refund
from app.persistence.db import build_engine, build_session_factory, session_scope
from app.persistence.memory_stores import OrderStore, RefundCreateOutcome
from app.persistence.models import AuditEventRow, OrderRow, RefundRow, TicketRow

# Statuses that occupy the same-order gate (one unresolved refund per
# order) and the ticket claim cap.
UNRESOLVED = ("pending", "unknown")
CLAIM_OCCUPYING = ("processed", "pending", "unknown")
# The terminal statuses finalize may move a refund to. Everything else is
# refused: an invalid transition target must not touch balances.
FINAL_STATUSES = (
    RefundStatus.PROCESSED,
    RefundStatus.FAILED,
    RefundStatus.REJECTED,
    RefundStatus.UNKNOWN,
)


class _UniqueConflict(Exception):
    """Internal: an INSERT/UPDATE hit a known unique constraint. The
    transaction is already rolled back by ``session_scope``; the caller
    re-checks the surviving row in a fresh transaction."""

    def __init__(self, constraint: str) -> None:
        super().__init__(constraint)
        self.constraint = constraint


def _constraint_name(exc: IntegrityError) -> str | None:
    """The PostgreSQL constraint name that fired (psycopg3 exposes it via
    ``diag.constraint_name``); falls back to the error text."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name:
        return name
    return str(orig) if orig else None


class PgRefundStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(
        self,
        orders: OrderStore | None,
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
        """Atomic: idempotency lookup + same-order gate + claim cap +
        balance reservation + PENDING insert + audit, in one transaction.
        Semantics mirror the in-memory store, with the same key matched
        before the same-order scan.

        ``ticket_claimed_amount`` is accepted for protocol parity but the
        claim is taken from the locked ticket row — never from the
        caller."""
        with session_scope(self._sessions) as s:
            existing = self._by_key(s, idempotency_key)
            if existing is not None:
                return self._existing_outcome(
                    existing,
                    order_id=order_id,
                    amount=amount,
                    ticket_id=ticket_id,
                    issue_type=issue_type,
                    plan_id=plan_id,
                )
            try:
                return self._create_new(
                    s,
                    order_id=order_id,
                    amount=amount,
                    idempotency_key=idempotency_key,
                    ticket_id=ticket_id,
                    issue_type=issue_type,
                    plan_id=plan_id,
                    run_id=run_id,
                    reason=reason,
                    now=now,
                )
            except _UniqueConflict as race:
                pass  # rolled back; resolve in a fresh transaction below
        # The insert lost a unique race. Re-read the surviving row in a
        # fresh transaction and answer with the same business kinds the
        # in-memory store would have produced.
        with session_scope(self._sessions) as s:
            if race.constraint == "uq_refunds_idempotency_key":
                existing = self._by_key(s, idempotency_key)
                if existing is None:
                    raise RuntimeError(
                        f"idempotency_key {idempotency_key} vanished after a "
                        "unique conflict; concurrent deletion is unsupported"
                    )
                return self._existing_outcome(
                    existing,
                    order_id=order_id,
                    amount=amount,
                    ticket_id=ticket_id,
                    issue_type=issue_type,
                    plan_id=plan_id,
                )
            # uq_refunds_order_unresolved: another refund of this order is
            # pending or unknown — report it precisely.
            gate = self._same_order_gate(s, order_id, exclude_refund_id=None)
            if gate is not None:
                kind, refund = gate
                return RefundCreateOutcome(kind=kind, refund=refund)
            return RefundCreateOutcome(kind="concurrent")

    def _create_new(
        self,
        s: Session,
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
        # Lock order: orders -> tickets -> refunds.
        order = s.execute(
            select(OrderRow)
            .where(OrderRow.order_id == order_id)
            .with_for_update()
        ).scalar_one_or_none()
        if order is None:
            return RefundCreateOutcome(kind="balance_exceeded")

        # Re-check the idempotency key now that the order lock serializes
        # us against the winner: the in-memory baseline matches a same-key
        # record ahead of the same-order scan, so a loser blocked behind
        # the winner's lock must resolve (replay/conflict per intent) the
        # same key instead of being pre-empted by the order gate.
        existing = self._by_key(s, idempotency_key)
        if existing is not None:
            return self._existing_outcome(
                existing,
                order_id=order_id,
                amount=amount,
                ticket_id=ticket_id,
                issue_type=issue_type,
                plan_id=plan_id,
            )

        ticket = s.execute(
            select(TicketRow)
            .where(TicketRow.ticket_id == ticket_id)
            .with_for_update()
        ).scalar_one_or_none()
        if ticket is None:
            # Database-level fallback, fail closed: the service layer
            # performs the TICKET_NOT_FOUND check before calling the
            # store, and a missing ticket row here means the caller
            # bypassed that contract. Mirroring the in-memory store's
            # claimless-ticket semantics (a ticket without a claim takes
            # no refunds), this reads as claim 0.
            #
            # The ticket's order binding is deliberately NOT checked: the
            # claim cap is the ticket-level invariant and, exactly as in
            # the in-memory store, it spans every refund of the ticket
            # regardless of order.
            return RefundCreateOutcome(kind="claim_exceeded")

        # A different idempotency key may still target the same order, but
        # an order with an unresolved refund takes no new refunds.
        gate = self._same_order_gate(s, order_id, exclude_refund_id=None)
        if gate is not None:
            kind, refund = gate
            return RefundCreateOutcome(kind=kind, refund=refund)

        # Balance pre-check (same ordering as the in-memory store: balance
        # before the claim cap).
        if order.refunded_amount >= order.total_amount:
            return RefundCreateOutcome(kind="already_refunded")
        if amount > order.total_amount - order.refunded_amount - order.reserved_amount:
            return RefundCreateOutcome(kind="balance_exceeded")

        # Claim cap, computed under the locked ticket row: only settled,
        # in-flight and unresolved refunds occupy the claim.
        claimed = (
            ticket.claimed_amount
            if ticket.claimed_amount is not None
            else Decimal("0")
        )
        occupied = s.scalar(
            select(func.coalesce(func.sum(RefundRow.amount), 0)).where(
                RefundRow.ticket_id == ticket_id,
                RefundRow.status.in_(CLAIM_OCCUPYING),
            )
        )
        if amount > claimed - occupied:
            return RefundCreateOutcome(kind="claim_exceeded")

        # Atomic reservation: one conditional UPDATE on the locked row.
        reserved = s.execute(
            update(OrderRow)
            .where(
                OrderRow.order_id == order_id,
                OrderRow.refunded_amount
                + OrderRow.reserved_amount
                + amount
                <= OrderRow.total_amount,
            )
            .values(reserved_amount=OrderRow.reserved_amount + amount)
        )
        if reserved.rowcount == 0:
            return RefundCreateOutcome(kind="balance_exceeded")

        row = RefundRow(
            refund_id=f"REF-{uuid4().hex[:12]}",
            idempotency_key=idempotency_key,
            ticket_id=ticket_id,
            order_id=order_id,
            amount=amount,
            currency=order.currency,
            issue_type=issue_type.value,
            plan_id=plan_id,
            run_id=run_id,
            status=RefundStatus.PENDING.value,
            reason=reason,
            created_at=now,
        )
        s.add(row)
        s.add(
            self._audit_row(
                "refund_store.create",
                ticket_id=ticket_id,
                order_id=order_id,
                run_id=run_id,
                now=now,
                details={
                    "refund_id": row.refund_id,
                    "idempotency_key": idempotency_key,
                    "amount": str(amount),
                    "status": row.status,
                },
            )
        )
        try:
            s.flush()
        except IntegrityError as exc:
            name = _constraint_name(exc)
            if name in ("uq_refunds_idempotency_key", "uq_refunds_order_unresolved"):
                raise _UniqueConflict(name) from exc
            raise  # FK/CHECK/anything else: propagate, never disguise
        return RefundCreateOutcome(kind="execute", refund=self._to_domain(row))

    # ------------------------------------------------------------------
    # finalize
    # ------------------------------------------------------------------

    def finalize(
        self,
        refund_id: str,
        status: RefundStatus,
        *,
        orders: OrderStore | None,
        now: datetime,
    ) -> Refund | None:
        """Move a PENDING/UNKNOWN refund to a terminal status. The row is
        locked and its current status re-checked under the lock, so two
        concurrent finalizers cannot both settle: the second one sees a
        terminal status and changes nothing. The balance moves in the
        same transaction: the reservation is released on FAILED/REJECTED,
        moved into the confirmed refunded amount on PROCESSED, and kept
        for UNKNOWN (fail closed). Reaching a terminal status also clears
        any recovery claim — a settled row has nothing left to recover."""
        with session_scope(self._sessions) as s:
            row = self._by_id(s, refund_id)
            if row is None:
                return None
            # Lock order: orders -> refunds (tickets are not needed here).
            order = s.execute(
                select(OrderRow)
                .where(OrderRow.order_id == row.order_id)
                .with_for_update()
            ).scalar_one_or_none()
            if order is None:
                return None  # FK-violating state; refuse rather than corrupt
            row = s.execute(
                select(RefundRow)
                .where(RefundRow.refund_id == refund_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if row is None or row.status not in UNRESOLVED:
                return None  # terminal already; never settle twice
            if status not in FINAL_STATUSES:
                # The in-memory store is laxer here; the database store
                # refuses invalid transition targets outright.
                return None
            self._move_balances(s, row, status)
            row.status = status.value
            if status is RefundStatus.PROCESSED:
                row.processed_at = now
            self._clear_recovery_claim(row)
            s.add(
                self._audit_row(
                    "refund_store.finalize",
                    ticket_id=row.ticket_id,
                    order_id=row.order_id,
                    run_id=row.run_id,
                    now=now,
                    details={
                        "refund_id": row.refund_id,
                        "amount": str(row.amount),
                        "status": row.status,
                    },
                )
            )
            s.flush()
            return self._to_domain(row)

    @staticmethod
    def _move_balances(s: Session, row: RefundRow, status: RefundStatus) -> None:
        """Balance movement for a terminal transition, on an already
        locked order row. FAILED/REJECTED release the reservation;
        PROCESSED moves it into the confirmed refunded amount; UNKNOWN
        moves nothing (fail closed, the reservation stays held)."""
        amount = row.amount
        if status in (RefundStatus.FAILED, RefundStatus.REJECTED):
            s.execute(
                update(OrderRow)
                .where(OrderRow.order_id == row.order_id)
                .values(
                    reserved_amount=func.greatest(
                        Decimal("0"), OrderRow.reserved_amount - amount
                    )
                )
            )
        elif status is RefundStatus.PROCESSED:
            s.execute(
                update(OrderRow)
                .where(OrderRow.order_id == row.order_id)
                .values(
                    reserved_amount=func.greatest(
                        Decimal("0"), OrderRow.reserved_amount - amount
                    ),
                    refunded_amount=OrderRow.refunded_amount + amount,
                )
            )

    @staticmethod
    def _clear_recovery_claim(row: RefundRow) -> None:
        row.recovery_claim_owner = None
        row.recovery_claim_expires_at = None

    # ------------------------------------------------------------------
    # recovery (Day 6): discovery, lease claim, lease-guarded settle
    # ------------------------------------------------------------------

    def find_recoverable(
        self, now: datetime, *, pending_grace_seconds: float
    ) -> tuple[Refund, ...]:
        """Refunds eligible for a recovery attempt, oldest first.

        - UNKNOWN rows are always eligible: no live executor owns an
          UNKNOWN row, and a repeated query is exactly what resolution
          needs.
        - PENDING rows only become eligible once they are older than
          ``pending_grace_seconds`` — a PENDING row younger than that may
          still belong to a live executor whose gateway call is in
          flight, and querying it early is wasted work (it is never
          unsafe: recovery only queries, and settling is idempotent).
        - A row whose recovery lease is still valid is not eligible; its
          claim expiry is the retry schedule.
        """
        grace_cutoff = now - timedelta(seconds=pending_grace_seconds)
        with session_scope(self._sessions) as s:
            rows = (
                s.execute(
                    select(RefundRow)
                    .where(
                        RefundRow.status.in_(UNRESOLVED),
                        or_(
                            RefundRow.recovery_claim_expires_at.is_(None),
                            RefundRow.recovery_claim_expires_at < now,
                        ),
                        or_(
                            RefundRow.status == RefundStatus.UNKNOWN.value,
                            RefundRow.created_at <= grace_cutoff,
                        ),
                    )
                    .order_by(RefundRow.created_at)
                )
                .scalars()
                .all()
            )
            return tuple(self._to_domain(row) for row in rows)

    def claim_recovery(
        self,
        refund_id: str,
        *,
        owner: str,
        expires_at: datetime,
        now: datetime,
    ) -> Literal["claimed", "terminal", "active_claim", "not_found"]:
        """Atomically take the recovery lease on one refund.

        One conditional UPDATE is the whole claim: only an unresolved
        row with no unexpired claim matches, so two concurrent claimers
        serialize on the row lock and exactly one gets ``rowcount == 1``.
        No lock is held after this transaction — the gateway query runs
        outside it. Expiry only permits another query, never another
        execution (recovery has no execute path at all)."""
        with session_scope(self._sessions) as s:
            claimed = s.execute(
                update(RefundRow)
                .where(
                    RefundRow.refund_id == refund_id,
                    RefundRow.status.in_(UNRESOLVED),
                    or_(
                        RefundRow.recovery_claim_expires_at.is_(None),
                        RefundRow.recovery_claim_expires_at < now,
                    ),
                )
                .values(
                    recovery_claim_owner=owner,
                    recovery_claim_expires_at=expires_at,
                    recovery_attempts=RefundRow.recovery_attempts + 1,
                )
            )
            if claimed.rowcount == 1:
                return "claimed"
            row = self._by_id(s, refund_id)
            if row is None:
                return "not_found"
            if row.status not in UNRESOLVED:
                return "terminal"
            return "active_claim"

    def settle_recovered(
        self,
        refund_id: str,
        *,
        expected_owner: str,
        status: RefundStatus,
        now: datetime,
        gateway_result: str,
    ) -> Literal["settled", "already_terminal", "lost_ownership", "not_found"]:
        """Lease-guarded settle from gateway evidence, in one transaction.

        The row is locked and re-read: if another worker settled it
        meanwhile, or the lease moved on (a newer worker now owns the
        claim), nothing is written — a late worker can never overwrite a
        newer terminal state. The balance movement is the same as
        ``finalize`` (PROCESSED moves the reservation into the confirmed
        refunded amount; FAILED releases it), the audit event carries the
        gateway evidence, and the lease is cleared."""
        if status not in (RefundStatus.PROCESSED, RefundStatus.FAILED):
            raise ValueError(
                f"recovery may only settle to PROCESSED or FAILED, got {status}"
            )
        with session_scope(self._sessions) as s:
            peek = self._by_id(s, refund_id)
            if peek is None:
                return "not_found"
            order = s.execute(
                select(OrderRow)
                .where(OrderRow.order_id == peek.order_id)
                .with_for_update()
            ).scalar_one_or_none()
            if order is None:
                return "not_found"  # FK-violating state; refuse
            row = s.execute(
                select(RefundRow)
                .where(RefundRow.refund_id == refund_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if row is None:
                return "not_found"
            if row.status not in UNRESOLVED:
                return "already_terminal"
            if row.recovery_claim_owner != expected_owner:
                # The lease moved on (expired and re-claimed) while our
                # gateway query ran. Our evidence is stale; abstain.
                return "lost_ownership"
            self._move_balances(s, row, status)
            row.status = status.value
            if status is RefundStatus.PROCESSED:
                row.processed_at = now
            self._clear_recovery_claim(row)
            row.last_recovery_error = None
            s.add(
                self._audit_row(
                    "refund_store.recovery_settle",
                    ticket_id=row.ticket_id,
                    order_id=row.order_id,
                    run_id=row.run_id,
                    now=now,
                    details={
                        "refund_id": row.refund_id,
                        "amount": str(row.amount),
                        "status": row.status,
                        "gateway_result": gateway_result,
                    },
                )
            )
            s.flush()
            return "settled"

    def mark_recovery_kept_unknown(
        self,
        refund_id: str,
        *,
        expected_owner: str,
        reason: str,
        now: datetime,
    ) -> Literal["recorded", "already_terminal", "lost_ownership", "not_found"]:
        """Record a recovery attempt that could not settle the refund.

        The reservation stays held, the status stays UNKNOWN/PENDING, the
        claim stays until its expiry (which is the retry schedule), and
        the reason lands on ``last_recovery_error`` for operators. A
        lease that has moved on (or a row that was settled) is never
        touched by this late worker."""
        with session_scope(self._sessions) as s:
            row = s.execute(
                select(RefundRow)
                .where(RefundRow.refund_id == refund_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if row is None:
                return "not_found"
            if row.status not in UNRESOLVED:
                return "already_terminal"
            if row.recovery_claim_owner != expected_owner:
                return "lost_ownership"
            row.last_recovery_error = reason[:512]
            s.add(
                self._audit_row(
                    "refund_store.recovery_kept_unknown",
                    ticket_id=row.ticket_id,
                    order_id=row.order_id,
                    run_id=row.run_id,
                    now=now,
                    details={
                        "refund_id": row.refund_id,
                        "status": row.status,
                        "reason": reason[:512],
                    },
                )
            )
            s.flush()
            return "recorded"

    # ------------------------------------------------------------------
    # reopen
    # ------------------------------------------------------------------

    def reopen(
        self,
        refund_id: str,
        *,
        orders: OrderStore | None,
        ticket_claimed_amount: Decimal | None,
    ) -> tuple[
        Literal[
            "reopened", "balance_exceeded", "claim_exceeded", "concurrent",
            "unresolved",
        ],
        Refund | None,
    ]:
        """Retry a transiently failed refund: re-check the same-order gate,
        the claim cap and the balance reservation, then move the record
        back to PENDING. The FAILED record itself is excluded from the
        gate scan. ``ticket_claimed_amount`` is protocol parity; the claim
        is read from the locked ticket row."""
        try:
            with session_scope(self._sessions) as s:
                return self._reopen_in_session(s, refund_id=refund_id)
        except _UniqueConflict:
            pass
        # The status UPDATE lost the same-order unique race (defensive:
        # the order-row lock serializes same-order reopeners, but the
        # partial unique index is the backstop). Re-read the gate.
        with session_scope(self._sessions) as s:
            row = self._by_id(s, refund_id)
            if row is None:
                return "balance_exceeded", None
            gate = self._same_order_gate(s, row.order_id, exclude_refund_id=refund_id)
            if gate is not None:
                return gate
            return "concurrent", None

    # ------------------------------------------------------------------
    # plain reads
    # ------------------------------------------------------------------

    def get(self, refund_id: str) -> Refund | None:
        """Read one refund by id in a fresh, short transaction. No lock."""
        with session_scope(self._sessions) as s:
            row = self._by_id(s, refund_id)
            return self._to_domain(row) if row is not None else None

    def get_by_key(self, idempotency_key: str) -> Refund | None:
        """Read the refund an idempotency key resolves to (None if unused)."""
        with session_scope(self._sessions) as s:
            row = self._by_key(s, idempotency_key)
            return self._to_domain(row) if row is not None else None

    def all(self) -> tuple[Refund, ...]:
        """Every refund, oldest first (creation order)."""
        with session_scope(self._sessions) as s:
            rows = (
                s.execute(
                    select(RefundRow).order_by(RefundRow.created_at)
                )
                .scalars()
                .all()
            )
            return tuple(self._to_domain(row) for row in rows)

    def count(self) -> int:
        with session_scope(self._sessions) as s:
            return s.scalar(select(func.count()).select_from(RefundRow)) or 0

    def _reopen_in_session(
        self, s: Session, *, refund_id: str
    ) -> tuple[
        Literal[
            "reopened", "balance_exceeded", "claim_exceeded", "concurrent",
            "unresolved",
        ],
        Refund | None,
    ]:
        # Unlocked peek first: reopen needs the order/ticket ids before it
        # can take locks in the canonical order.
        peek = self._by_id(s, refund_id)
        if peek is None or peek.status != RefundStatus.FAILED.value:
            return "balance_exceeded", None
        # Lock order: orders -> tickets -> refunds.
        order = s.execute(
            select(OrderRow)
            .where(OrderRow.order_id == peek.order_id)
            .with_for_update()
        ).scalar_one_or_none()
        if order is None:
            return "balance_exceeded", None
        ticket = s.execute(
            select(TicketRow)
            .where(TicketRow.ticket_id == peek.ticket_id)
            .with_for_update()
        ).scalar_one_or_none()
        if ticket is None:
            return "claim_exceeded", None
        row = s.execute(
            select(RefundRow)
            .where(RefundRow.refund_id == refund_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if row is None or row.status != RefundStatus.FAILED.value:
            return "balance_exceeded", None

        # Same-order unresolved gate, the FAILED record excluded — the
        # one gate the same-key FAILED branch of create does not re-check.
        gate = self._same_order_gate(s, row.order_id, exclude_refund_id=refund_id)
        if gate is not None:
            return gate

        # Claim cap: only PROCESSED/PENDING/UNKNOWN records occupy it; the
        # FAILED record being reopened holds no reservation and is
        # excluded, so the retry must fit on top of everything else.
        claimed = (
            ticket.claimed_amount
            if ticket.claimed_amount is not None
            else Decimal("0")
        )
        occupied = s.scalar(
            select(func.coalesce(func.sum(RefundRow.amount), 0)).where(
                RefundRow.ticket_id == row.ticket_id,
                RefundRow.status.in_(CLAIM_OCCUPYING),
            )
        )
        if row.amount > claimed - occupied:
            return "claim_exceeded", None

        # Balance: friendly check plus the conditional reservation UPDATE
        # (the order row is already locked).
        if row.amount > order.total_amount - order.refunded_amount - order.reserved_amount:
            return "balance_exceeded", None
        reserved = s.execute(
            update(OrderRow)
            .where(
                OrderRow.order_id == row.order_id,
                OrderRow.refunded_amount
                + OrderRow.reserved_amount
                + row.amount
                <= OrderRow.total_amount,
            )
            .values(reserved_amount=OrderRow.reserved_amount + row.amount)
        )
        if reserved.rowcount == 0:
            return "balance_exceeded", None

        row.status = RefundStatus.PENDING.value
        s.add(
            self._audit_row(
                "refund_store.reopen",
                ticket_id=row.ticket_id,
                order_id=row.order_id,
                run_id=row.run_id,
                # reopen carries no clock argument; the audit timestamp is
                # the conversion's UTC time.
                now=datetime.now(timezone.utc),
                details={
                    "refund_id": row.refund_id,
                    "amount": str(row.amount),
                    "status": row.status,
                },
            )
        )
        try:
            s.flush()
        except IntegrityError as exc:
            name = _constraint_name(exc)
            if name == "uq_refunds_order_unresolved":
                raise _UniqueConflict(name) from exc
            raise
        return "reopened", self._to_domain(row)

    # ------------------------------------------------------------------
    # shared helpers
    # ------------------------------------------------------------------

    def _existing_outcome(
        self,
        row: RefundRow,
        *,
        order_id: str,
        amount: Decimal,
        ticket_id: str,
        issue_type: IssueType,
        plan_id: str | None,
    ) -> RefundCreateOutcome:
        """The same-key branch of create, same ordering and semantics as
        the in-memory store: full-intent comparison first, then the
        status-specific paths."""
        if (
            row.order_id != order_id
            or row.amount != amount
            or row.ticket_id != ticket_id
            or IssueType(row.issue_type) is not issue_type
            or row.plan_id != plan_id
        ):
            return RefundCreateOutcome(kind="conflict")
        status = RefundStatus(row.status)
        if status is RefundStatus.PROCESSED:
            return RefundCreateOutcome(kind="already_processed", refund=self._to_domain(row))
        if status is RefundStatus.REJECTED:
            return RefundCreateOutcome(kind="gateway_rejected", refund=self._to_domain(row))
        if status is RefundStatus.UNKNOWN:
            return RefundCreateOutcome(kind="needs_resolution", refund=self._to_domain(row))
        if status is RefundStatus.FAILED:
            return RefundCreateOutcome(kind="retry", refund=self._to_domain(row))
        # PENDING (or an unexpected value): another request is executing.
        return RefundCreateOutcome(kind="concurrent", refund=self._to_domain(row))

    def _same_order_gate(
        self, s: Session, order_id: str, *, exclude_refund_id: str | None
    ) -> tuple[Literal["concurrent", "unresolved"], Refund | None] | None:
        """The same-order unresolved gate: another refund of this order
        that is PENDING blocks as concurrent, UNKNOWN blocks as unresolved
        (carrying the blocking record). None means the order is clear."""
        rows = s.execute(
            select(RefundRow).where(
                RefundRow.order_id == order_id,
                RefundRow.status.in_(UNRESOLVED),
            )
        ).scalars().all()
        for row in rows:
            if exclude_refund_id is not None and row.refund_id == exclude_refund_id:
                continue
            if row.status == RefundStatus.PENDING.value:
                return "concurrent", None
        for row in rows:
            if exclude_refund_id is not None and row.refund_id == exclude_refund_id:
                continue
            if row.status == RefundStatus.UNKNOWN.value:
                return "unresolved", self._to_domain(row)
        return None

    def _by_key(self, s: Session, idempotency_key: str) -> RefundRow | None:
        return s.execute(
            select(RefundRow).where(RefundRow.idempotency_key == idempotency_key)
        ).scalar_one_or_none()

    def _by_id(self, s: Session, refund_id: str) -> RefundRow | None:
        return s.execute(
            select(RefundRow).where(RefundRow.refund_id == refund_id)
        ).scalar_one_or_none()

    def _audit_row(
        self,
        action: str,
        *,
        ticket_id: str | None,
        order_id: str | None,
        run_id: str | None,
        now: datetime,
        details: dict,
    ) -> AuditEventRow:
        """Audit event for a refund state change, committed in the same
        transaction as the change. details carry refund linkage only —
        no secrets, no customer-sensitive fields beyond the ids already on
        the row."""
        return AuditEventRow(
            event_id=f"EVT-{uuid4().hex[:12]}",
            timestamp=now,
            actor="system",
            action=action,
            ticket_id=ticket_id,
            order_id=order_id,
            run_id=run_id,
            details=details,
        )

    def _to_domain(self, row: RefundRow) -> Refund:
        return Refund(
            refund_id=row.refund_id,
            idempotency_key=row.idempotency_key,
            ticket_id=row.ticket_id,
            order_id=row.order_id,
            amount=row.amount,
            currency=row.currency,
            issue_type=IssueType(row.issue_type),
            plan_id=row.plan_id,
            run_id=row.run_id,
            status=RefundStatus(row.status),
            reason=row.reason,
            created_at=row.created_at,
            processed_at=row.processed_at,
        )


def build_pg_refund_store(settings: Settings) -> PgRefundStore:
    """Explicit PostgreSQL wiring: the URL in settings decides the backend.
    There is no fallback — a connection failure raises, it never silently
    swaps to the in-memory store."""
    return PgRefundStore(build_session_factory(build_engine(settings)))
