"""Shared refund-store contract for the in-memory and PostgreSQL stores.

Both implementations expose the same three write operations (plus plain
reads) with the same signatures, return types and state-machine
semantics, so the service layer can take either without branching.

``orders`` is part of the contract for interface parity: the in-memory
store needs the live :class:`~app.persistence.memory_stores.OrderStore`
to reserve and settle balances, while the PostgreSQL store keeps order
state in the ``orders`` table and ignores the argument (documented on
:class:`~app.persistence.pg_refund_store.PgRefundStore`). The service
layer always passes its wired order store.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

from app.domain.enums import IssueType, RefundStatus
from app.domain.models import Refund
from app.persistence.memory_stores import OrderStore, RefundCreateOutcome


class RefundStoreProtocol(Protocol):
    """Contract both refund stores satisfy. Semantics per operation are
    the in-memory store's: same-key intent comparison, same-order
    unresolved gate, claim cap, atomic reservation, state-machine guards."""

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
    ) -> RefundCreateOutcome: ...

    def finalize(
        self,
        refund_id: str,
        status: RefundStatus,
        *,
        orders: OrderStore | None,
        now: datetime,
    ) -> Refund | None: ...

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
    ]: ...

    # --- plain reads (no locking, no side effects) ---
    #
    # The agent graph re-reads persisted state after execution (never
    # trusting the ToolResult alone), so every backend must expose these.

    def get(self, refund_id: str) -> Refund | None: ...

    def get_by_key(self, idempotency_key: str) -> Refund | None: ...

    def all(self) -> tuple[Refund, ...]: ...

    def count(self) -> int: ...
