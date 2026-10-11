"""Store implementations for the PostgreSQL backend.

When ``store_backend=postgres``, the agent graph is wired with these
stores so that every piece of state the refund chain depends on lives in
PostgreSQL:

- :class:`PgOrderStore` — orders read from the ``orders`` table. The
  refunded/reserved balances on that row are maintained exclusively by
  :class:`~app.persistence.pg_refund_store.PgRefundStore`; balance
  mutations here raise instead of silently doing nothing, so a wiring
  mistake fails loudly.
- :class:`PgPlanStore` — the trusted plan registry on ``plans``, with the
  same content-hash contract as the in-memory ``PlanStore``. Required:
  ``refunds.plan_id`` is a real foreign key to ``plans``.
- :class:`PgAuditStore` — graph-level audit events on ``audit_events``.
- :class:`PgAgentRunStore` — run bookkeeping on ``agent_runs``. Required:
  ``audit_events.run_id`` is a real foreign key to ``agent_runs``.

Workflow state that is *not* part of the refund chain — tickets, ticket
notes, shipments, approvals — stays in-memory even on this backend (see
README, "持久化边界"). A run on a ticket that has no row in ``tickets``
fails loudly at the first insert (foreign key), never silently degrades.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import AgentRunStatus, OrderStatus
from app.domain.models import AgentRun, AuditEvent, Order, ResolutionPlan
from app.persistence.db import session_scope
from app.persistence.models import (
    AgentRunRow,
    AuditEventRow,
    OrderRow,
    PlanRow,
)
from app.services.plans import StoredPlan, plan_content_hash


class PgOrderStore:
    """Orders read from PostgreSQL. The refunded/reserved balances come
    from the row that :class:`PgRefundStore` maintains under row locks —
    never from a caller. Read-only by design: balance mutations are owned
    by the refund store's transaction."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def get(self, order_id: str) -> Order | None:
        with session_scope(self._sessions) as s:
            row = s.get(OrderRow, order_id)
            if row is None:
                return None
            return Order(
                order_id=row.order_id,
                customer_id=row.customer_id,
                status=OrderStatus(row.status),
                currency=row.currency,
                total_amount=row.total_amount,
                refunded_amount=row.refunded_amount,
                reserved_amount=row.reserved_amount,
                created_at=row.created_at,
                delivered_at=row.delivered_at,
            )

    def reserve(self, order_id: str, amount: Decimal) -> Order | None:
        raise NotImplementedError(
            "PgOrderStore is read-through: balance reservations are owned "
            "by PgRefundStore inside its transaction"
        )

    def release(self, order_id: str, amount: Decimal) -> None:
        raise NotImplementedError(
            "PgOrderStore is read-through: reservation release is owned "
            "by PgRefundStore inside its transaction"
        )

    def settle(self, order_id: str, amount: Decimal) -> None:
        raise NotImplementedError(
            "PgOrderStore is read-through: settlement is owned by "
            "PgRefundStore inside its transaction"
        )


class PgPlanStore:
    """Trusted plan registry on the ``plans`` table.

    Same contract as the in-memory ``PlanStore``: registering an existing
    ``plan_id`` with different content fails closed, ``load_verified``
    recomputes the content hash before returning anything, and a reused
    plan id can never shadow the content an approval was granted for.

    One documented divergence: ``run_id`` is not a ``plans`` column, so
    plans read back carry ``run_id=None``."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def register(
        self,
        plan: ResolutionPlan,
        *,
        run_id: str | None,
        created_at: datetime,
    ) -> StoredPlan:
        stored = StoredPlan(
            plan_id=plan.plan_id,
            ticket_id=plan.ticket_id,
            content=plan,
            content_hash=plan_content_hash(plan),
            run_id=run_id,
            created_at=created_at,
        )
        try:
            with session_scope(self._sessions) as s:
                existing = s.get(PlanRow, plan.plan_id)
                if existing is not None:
                    if existing.content_hash != stored.content_hash:
                        raise ValueError(
                            f"plan_id {plan.plan_id} is already registered "
                            "with different content"
                        )
                    return stored
                s.add(
                    PlanRow(
                        plan_id=plan.plan_id,
                        ticket_id=plan.ticket_id,
                        content=plan.model_dump(mode="json"),
                        content_hash=stored.content_hash,
                        created_at=created_at,
                    )
                )
                return stored
        except IntegrityError:
            # Concurrent registration of the same plan_id: the surviving
            # row decides, exactly like the primary key would. Anything
            # other than that (e.g. a missing ticket row) re-raises below.
            with session_scope(self._sessions) as s:
                row = s.get(PlanRow, plan.plan_id)
                if row is None:
                    raise
                if row.content_hash != stored.content_hash:
                    raise ValueError(
                        f"plan_id {plan.plan_id} is already registered "
                        "with different content"
                    ) from None
                return stored

    def get(self, plan_id: str) -> StoredPlan | None:
        with session_scope(self._sessions) as s:
            return self._to_domain(s.get(PlanRow, plan_id))

    def load_verified(self, plan_id: str) -> StoredPlan | None:
        """Load and re-verify: recompute the hash of the stored content and
        refuse to return the plan if it no longer matches (drift)."""
        stored = self.get(plan_id)
        if stored is None:
            return None
        if plan_content_hash(stored.content) != stored.content_hash:
            return None
        return stored

    def verify(self, plan_id: str) -> Literal["ok", "missing", "drift"]:
        stored = self.get(plan_id)
        if stored is None:
            return "missing"
        if plan_content_hash(stored.content) != stored.content_hash:
            return "drift"
        return "ok"

    def all(self) -> tuple[StoredPlan, ...]:
        with session_scope(self._sessions) as s:
            rows = s.execute(select(PlanRow).order_by(PlanRow.created_at)).scalars().all()
            return tuple(self._to_domain(row) for row in rows)

    def _to_domain(self, row: PlanRow | None) -> StoredPlan | None:
        if row is None:
            return None
        return StoredPlan(
            plan_id=row.plan_id,
            ticket_id=row.ticket_id,
            content=ResolutionPlan.model_validate(row.content),
            content_hash=row.content_hash,
            run_id=None,  # plans has no run_id column (see class docstring)
            created_at=row.created_at,
        )


class PgAuditStore:
    """Append-only graph audit trail on ``audit_events``.

    Each event commits in its own short transaction. The refund-state
    audit rows written by :class:`PgRefundStore` commit in the *same*
    transaction as the state change; these graph-level events are separate
    appends by design (they describe the run, not a balance change)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def add(self, event: AuditEvent) -> None:
        with session_scope(self._sessions) as s:
            s.add(
                AuditEventRow(
                    event_id=event.event_id,
                    timestamp=event.timestamp,
                    actor=event.actor,
                    action=event.action,
                    ticket_id=event.ticket_id,
                    order_id=event.order_id,
                    run_id=event.run_id,
                    details=dict(event.details),
                )
            )

    def all(self) -> tuple[AuditEvent, ...]:
        with session_scope(self._sessions) as s:
            rows = (
                s.execute(
                    select(AuditEventRow).order_by(
                        AuditEventRow.timestamp, AuditEventRow.event_id
                    )
                )
                .scalars()
                .all()
            )
            return tuple(self._to_domain(row) for row in rows)

    def _to_domain(self, row: AuditEventRow) -> AuditEvent:
        return AuditEvent(
            event_id=row.event_id,
            timestamp=row.timestamp,
            actor=row.actor,
            action=row.action,
            ticket_id=row.ticket_id,
            order_id=row.order_id,
            run_id=row.run_id,
            details=dict(row.details),
        )


class PgAgentRunStore:
    """Run bookkeeping on ``agent_runs``.

    ``update`` mirrors the in-memory store: it reloads the row into the
    domain model and applies the same constraint re-validation before
    writing back, so an update that violates the model (or the table's
    CHECKs) is rejected instead of persisted."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def create(self, run: AgentRun) -> None:
        with session_scope(self._sessions) as s:
            s.add(
                AgentRunRow(
                    run_id=run.run_id,
                    ticket_id=run.ticket_id,
                    status=run.status.value,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    iterations=run.iterations,
                    max_iterations=run.max_iterations,
                    error=run.error,
                )
            )

    def get(self, run_id: str) -> AgentRun | None:
        with session_scope(self._sessions) as s:
            return self._to_domain(s.get(AgentRunRow, run_id))

    def all(self) -> tuple[AgentRun, ...]:
        with session_scope(self._sessions) as s:
            rows = (
                s.execute(select(AgentRunRow).order_by(AgentRunRow.started_at))
                .scalars()
                .all()
            )
            return tuple(self._to_domain(row) for row in rows)

    def update(
        self,
        run_id: str,
        *,
        status: AgentRunStatus | None = None,
        iterations: int | None = None,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> AgentRun | None:
        with session_scope(self._sessions) as s:
            row = s.get(AgentRunRow, run_id)
            if row is None:
                return None
            current = self._to_domain(row)
            assert current is not None
            updates: dict = {}
            if status is not None:
                updates["status"] = status
            if iterations is not None:
                updates["iterations"] = iterations
            if error is not None:
                updates["error"] = error
            if finished_at is not None:
                updates["finished_at"] = finished_at
            updated = current.model_copy(update=updates)  # re-validates constraints
            row.status = updated.status.value
            row.iterations = updated.iterations
            row.max_iterations = updated.max_iterations
            row.error = updated.error
            row.finished_at = updated.finished_at
            return updated

    def _to_domain(self, row: AgentRunRow | None) -> AgentRun | None:
        if row is None:
            return None
        return AgentRun(
            run_id=row.run_id,
            ticket_id=row.ticket_id,
            status=AgentRunStatus(row.status),
            started_at=row.started_at,
            finished_at=row.finished_at,
            iterations=row.iterations,
            max_iterations=row.max_iterations,
            error=row.error,
        )
