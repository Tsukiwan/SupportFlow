"""Refund service: the safety boundary around simulated refunds.

Every check is independent of LLM judgement and of caller-provided state:

1. the ticket and the order exist in their stores; the ticket must be bound
   to the requested order (TICKET_ORDER_MISMATCH otherwise);
2. the issue type and the policy are evaluated from the trusted ticket
   record, never from the caller's claims — a mismatching ``issue_type``
   argument is rejected up front (ISSUE_TYPE_MISMATCH);
3. order status and the live refundable balance come from the order store
   (never from the caller) and the balance is reserved atomically inside
   the refund store;
4. amount validity (positive, Decimal, at most 2 decimals) is enforced by
   ``CreateRefundInput`` before this service runs;
5. approval is required whenever the effective risk demands it (the
   higher of the ticket's risk level and the policy's required approval
   level; HIGH-or-above always requires approval, independently of the
   agent graph). An APPROVED approval must exist for the same ticket
   whose order, amount, plan version and plan content hash match exactly
   (a missing hash fails closed whenever a plan store is wired in), at
   least at the effective risk level, and within its validity window
   (``expires_at``);
6. idempotency and concurrency are enforced by the refund store's atomic
   section, not by in-memory ifs; the idempotency key binds the full
   operation intent, an order with a PENDING or UNKNOWN refund takes
   no new refunds (different idempotency key) until the original
   operation is settled, and the ticket's cumulative refunds
   (settled, in-flight and unresolved) may not exceed its claim.

Gateway result semantics: an exception says nothing about whether the
gateway executed the refund (a connection can drop after the money moved),
so exceptions are treated as UNKNOWN — the reservation is kept and a
same-key retry resolves the outcome by querying the original operation.
Only an explicit ``TRANSIENT_FAILURE`` outcome counts as "confirmed not
executed" and takes the recoverable-failure path. Cancellation is likewise
treated as UNKNOWN, never as a definitive failure.

Outcome categories map to distinct ToolErrorCodes: business rejection
(POLICY_DENIED / AMOUNT_MISMATCH / ALREADY_REFUNDED / CLAIM_EXCEEDED /
APPROVAL_* / IDEMPOTENCY_CONFLICT / TICKET_ORDER_MISMATCH /
ISSUE_TYPE_MISMATCH / GATEWAY_REJECTED), transient failure
(UPSTREAM_TIMEOUT / CONCURRENT_PROCESSING, both retryable), execution
failure (INTERNAL_ERROR) and unknown outcome (UNKNOWN_OUTCOME, resolved
by re-querying the gateway — never by assuming failure and creating a
new refund).
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.domain.enums import ApprovalStatus, RefundStatus, RISK_SEVERITY
from app.domain.models import Approval, Order, Refund
from app.persistence.memory_stores import (
    ApprovalStore,
    OrderStore,
    TicketStore,
)
from app.persistence.refund_store import RefundStoreProtocol
from app.services.approvals import latest_approval
from app.services.audit import AuditRecorder
from app.services.payments import GatewayOutcome, PaymentGateway
from app.services.plans import PlanStore
from app.services.refund_policy import (
    approval_requirement,
    evaluate_refund_policy,
)
from app.tools.context import ToolContext
from app.tools.result import ToolErrorCode, ToolResult
from app.tools.schemas import CreateRefundInput

logger = logging.getLogger(__name__)


class RefundService:
    def __init__(
        self,
        *,
        orders: OrderStore,
        refunds: RefundStoreProtocol,
        tickets: TicketStore,
        approvals: ApprovalStore,
        gateway: PaymentGateway,
        recorder: AuditRecorder,
        plans: PlanStore | None = None,
    ) -> None:
        self._orders = orders
        self._refunds = refunds
        self._tickets = tickets
        self._approvals = approvals
        self._gateway = gateway
        self._recorder = recorder
        self._plans = plans

    async def create(
        self, params: CreateRefundInput, ctx: ToolContext | None
    ) -> ToolResult[Refund]:
        ticket_id = ctx.ticket_id if ctx else None
        if not ticket_id:
            return ToolResult.error(
                ToolErrorCode.INVALID_INPUT,
                "create_refund requires a ticket context (ticket_id)",
            )

        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return ToolResult.error(
                ToolErrorCode.TICKET_NOT_FOUND,
                f"ticket {ticket_id} not found",
            )

        order = self._orders.get(params.order_id)
        if order is None:
            return ToolResult.error(
                ToolErrorCode.ORDER_NOT_FOUND,
                f"order {params.order_id} not found",
            )

        if ticket.order_id != params.order_id:
            return ToolResult.error(
                ToolErrorCode.TICKET_ORDER_MISMATCH,
                f"ticket {ticket_id} is bound to order {ticket.order_id}, "
                f"not {params.order_id}",
            )

        # The issue type must come from the ticket record — the caller's
        # argument is only accepted when it agrees with the trusted value.
        if ticket.issue_type is not params.issue_type:
            return ToolResult.error(
                ToolErrorCode.ISSUE_TYPE_MISMATCH,
                f"issue_type {params.issue_type.value} does not match the "
                f"ticket record ({ticket.issue_type.value})",
            )

        policy = evaluate_refund_policy(ticket.issue_type, order.status)
        if not policy.allowed:
            return ToolResult.error(
                ToolErrorCode.POLICY_DENIED,
                f"refund not allowed: {policy.reason}",
            )

        # The approval requirement is computed here from the trusted
        # ticket record exactly as the agent graph computes it (shared
        # function): effective risk = max(ticket risk, policy level), and
        # HIGH-or-above always requires approval — independent of the
        # graph, so callers that bypass it are still stopped.
        effective_risk, requires_approval = approval_requirement(
            ticket.risk_level, policy
        )
        if requires_approval:
            approval_check = self._verify_approval(
                params, order, ticket_id, effective_risk
            )
            if not approval_check.success:
                return approval_check
            approval = approval_check.data
            await self._recorder.record(
                tool="create_refund.approval_verified",
                ctx=ctx,
                outcome="success",
                details={
                    "approval_id": approval.approval_id,
                    "order_id": order.order_id,
                },
            )

        now = datetime.now(timezone.utc)
        outcome = self._refunds.create(
            self._orders,
            order_id=params.order_id,
            amount=params.amount,
            idempotency_key=params.idempotency_key,
            ticket_id=ticket_id,
            issue_type=ticket.issue_type,
            plan_id=params.plan_id,
            run_id=ctx.run_id if ctx else None,
            reason=policy.reason,
            ticket_claimed_amount=ticket.claimed_amount,
            now=now,
        )
        try:
            kind = outcome.kind
            if kind == "conflict":
                return ToolResult.error(
                    ToolErrorCode.IDEMPOTENCY_CONFLICT,
                    "idempotency_key was already used with a different operation intent",
                )
            if kind == "balance_exceeded":
                return ToolResult.error(
                    ToolErrorCode.AMOUNT_MISMATCH,
                    "amount exceeds the current refundable balance",
                )
            if kind == "already_refunded":
                return ToolResult.error(
                    ToolErrorCode.ALREADY_REFUNDED,
                    "order has already been fully refunded",
                )
            if kind == "claim_exceeded":
                return ToolResult.error(
                    ToolErrorCode.CLAIM_EXCEEDED,
                    "cumulative refunds for this ticket would exceed its claimed amount",
                )
            if kind == "concurrent":
                return ToolResult.error(
                    ToolErrorCode.CONCURRENT_PROCESSING,
                    "order is being processed by another request",
                    retryable=True,
                )
            if kind == "unresolved":
                return ToolResult.error(
                    ToolErrorCode.UNRESOLVED_REFUND,
                    f"order has an unresolved refund ({outcome.refund.refund_id}); "
                    "resolve it with the original idempotency_key before "
                    "starting a new refund",
                    retryable=False,
                    data=outcome.refund,
                )
            if kind == "already_processed":
                return ToolResult.ok(
                    "refund already processed for this idempotency_key",
                    data=outcome.refund,
                )
            if kind == "gateway_rejected":
                return ToolResult.error(
                    ToolErrorCode.GATEWAY_REJECTED,
                    "payment gateway rejected this refund",
                )
            if kind == "needs_resolution":
                return await self._resolve_unknown(outcome.refund)
            if kind == "retry":
                reopen_kind, reopened = self._refunds.reopen(
                    outcome.refund.refund_id,
                    orders=self._orders,
                    ticket_claimed_amount=ticket.claimed_amount,
                )
                if reopen_kind == "claim_exceeded":
                    return ToolResult.error(
                        ToolErrorCode.CLAIM_EXCEEDED,
                        "cumulative refunds for this ticket would exceed its claimed amount",
                    )
                if reopen_kind == "concurrent":
                    return ToolResult.error(
                        ToolErrorCode.CONCURRENT_PROCESSING,
                        "order is being processed by another request",
                        retryable=True,
                    )
                if reopen_kind == "unresolved":
                    return ToolResult.error(
                        ToolErrorCode.UNRESOLVED_REFUND,
                        f"order has an unresolved refund ({reopened.refund_id}); "
                        "resolve it with the original idempotency_key before "
                        "retrying this refund",
                        retryable=False,
                        data=reopened,
                    )
                if reopened is None:
                    return ToolResult.error(
                        ToolErrorCode.AMOUNT_MISMATCH,
                        "balance changed since the previous attempt",
                    )
                return await self._execute(reopened)
            return await self._execute(outcome.refund)
        except asyncio.CancelledError:
            # Cancellation says nothing about whether the gateway applied
            # the refund. Keep the record recoverable as UNKNOWN (the
            # reservation stays held, fail closed) so a same-key retry
            # resolves it through a gateway status query. Never leave a
            # PENDING record with no owner to finish it.
            if outcome.refund is not None:
                self._refunds.finalize(
                    outcome.refund.refund_id,
                    RefundStatus.UNKNOWN,
                    orders=self._orders,
                    now=datetime.now(timezone.utc),
                )
            raise

    def _verify_approval(
        self,
        params: CreateRefundInput,
        order: Order,
        ticket_id: str,
        effective_risk,
    ) -> ToolResult[Approval]:
        """Fail-closed approval gate. Only an APPROVED approval whose
        order, amount, plan version and plan content match exactly, whose
        risk level is at least the effective risk required for this refund
        (ticket risk raised to the policy level), and which is still within
        its validity window unlocks execution."""
        candidates = [
            a
            for a in self._approvals.all()
            if a.action == "refund" and a.ticket_id == ticket_id
        ]
        if not candidates:
            return ToolResult.error(
                ToolErrorCode.APPROVAL_REQUIRED,
                "refund policy requires human approval; none was requested for this ticket",
            )
        latest = latest_approval(candidates)
        if latest.status is ApprovalStatus.PENDING:
            return ToolResult.error(
                ToolErrorCode.APPROVAL_REQUIRED,
                "approval for this ticket is still pending",
            )
        if latest.status is ApprovalStatus.REJECTED:
            return ToolResult.error(
                ToolErrorCode.APPROVAL_REJECTED,
                "approval for this ticket was rejected",
            )
        if latest.status is ApprovalStatus.EXPIRED:
            return ToolResult.error(
                ToolErrorCode.APPROVAL_EXPIRED,
                "approval for this ticket has expired",
            )
        if (
            latest.expires_at is not None
            and datetime.now(timezone.utc) > latest.expires_at
        ):
            return ToolResult.error(
                ToolErrorCode.APPROVAL_EXPIRED,
                "approval for this ticket is past its validity window",
            )
        if latest.order_id != order.order_id:
            return ToolResult.error(
                ToolErrorCode.APPROVAL_MISMATCH,
                "approval was granted for a different order",
            )
        if latest.amount != params.amount:
            return ToolResult.error(
                ToolErrorCode.APPROVAL_MISMATCH,
                "approval amount does not match the requested amount",
            )
        if params.plan_id is None or latest.plan_id != params.plan_id:
            return ToolResult.error(
                ToolErrorCode.APPROVAL_MISMATCH,
                "approval plan version does not match the requested plan",
            )
        if RISK_SEVERITY[latest.risk_level] < RISK_SEVERITY[effective_risk]:
            return ToolResult.error(
                ToolErrorCode.APPROVAL_MISMATCH,
                "approval risk level is below the level required for this refund",
            )
        # Plan-content binding: the approval must cover the exact content
        # the human reviewed. Loaded from the trusted plan store (never
        # from caller claims), with the hash re-verified on load. Whenever
        # a plan store is wired in (always, in production) a missing hash
        # fails closed — an approval that does not provably bind the
        # registered plan never unlocks execution. The hash-less path is
        # reachable only by Day 2 test fixtures built without a plan store.
        if self._plans is not None:
            if latest.plan_content_hash is None:
                return ToolResult.error(
                    ToolErrorCode.APPROVAL_MISMATCH,
                    "approval carries no plan content hash; it cannot be "
                    "verified against the registered plan",
                )
            stored = self._plans.load_verified(latest.plan_id)
            if stored is None:
                return ToolResult.error(
                    ToolErrorCode.APPROVAL_MISMATCH,
                    "approval references a plan that is not registered or "
                    "whose content failed integrity verification",
                )
            if stored.content_hash != latest.plan_content_hash:
                return ToolResult.error(
                    ToolErrorCode.APPROVAL_MISMATCH,
                    "approval plan content hash does not match the registered plan",
                )
            plan = stored.content
            if (
                plan.ticket_id != latest.ticket_id
                or plan.order_id != latest.order_id
                or plan.action != latest.action
                or plan.amount != latest.amount
                or RISK_SEVERITY[plan.risk_level]
                != RISK_SEVERITY[latest.risk_level]
            ):
                return ToolResult.error(
                    ToolErrorCode.APPROVAL_MISMATCH,
                    "approval was granted for different plan content",
                )
        return ToolResult.ok("approval verified", data=latest)

    async def _execute(self, refund: Refund) -> ToolResult[Refund]:
        try:
            gateway_outcome = await self._gateway.execute(refund)
        except Exception:
            # An exception carries no proof the gateway did NOT execute the
            # refund (a connection can drop after the money moved). Treat it
            # as UNKNOWN: keep the reservation and let a same-key retry
            # resolve the outcome through a status query.
            logger.exception(
                "gateway raised during refund execution refund_id=%s",
                refund.refund_id,
            )
            self._refunds.finalize(
                refund.refund_id,
                RefundStatus.UNKNOWN,
                orders=self._orders,
                now=datetime.now(timezone.utc),
            )
            return ToolResult.error(
                ToolErrorCode.UNKNOWN_OUTCOME,
                "gateway call failed and the refund outcome is unknown; "
                "retry with the same idempotency_key to resolve it",
                retryable=False,
            )
        return self._apply_gateway_outcome(refund, gateway_outcome)

    async def _resolve_unknown(self, refund: Refund) -> ToolResult[Refund]:
        """An UNKNOWN refund must be resolved by querying the original
        operation — never by assuming failure and creating a new refund."""
        try:
            gateway_outcome = await self._gateway.status(refund.refund_id)
        except Exception:
            logger.exception(
                "gateway status query failed refund_id=%s", refund.refund_id
            )
            return ToolResult.error(
                ToolErrorCode.UNKNOWN_OUTCOME,
                "could not determine the previous refund outcome; retry later",
                retryable=False,
            )
        return self._apply_gateway_outcome(refund, gateway_outcome)

    def _apply_gateway_outcome(
        self, refund: Refund, gateway_outcome: GatewayOutcome
    ) -> ToolResult[Refund]:
        now = datetime.now(timezone.utc)
        if gateway_outcome is GatewayOutcome.PROCESSED:
            updated = self._refunds.finalize(
                refund.refund_id, RefundStatus.PROCESSED, orders=self._orders, now=now
            ) or self._refunds.get(refund.refund_id)
            return ToolResult.ok("refund processed", data=updated)
        if gateway_outcome is GatewayOutcome.TRANSIENT_FAILURE:
            self._refunds.finalize(
                refund.refund_id, RefundStatus.FAILED, orders=self._orders, now=now
            )
            return ToolResult.error(
                ToolErrorCode.UPSTREAM_TIMEOUT,
                "payment gateway temporarily unavailable; retry with the same idempotency_key",
                retryable=True,
            )
        if gateway_outcome is GatewayOutcome.REJECTED:
            self._refunds.finalize(
                refund.refund_id, RefundStatus.REJECTED, orders=self._orders, now=now
            )
            return ToolResult.error(
                ToolErrorCode.GATEWAY_REJECTED,
                "payment gateway rejected the refund",
            )
        # UNKNOWN: keep the reservation (fail closed) and require a
        # re-query on the next attempt with the same idempotency_key.
        self._refunds.finalize(
            refund.refund_id, RefundStatus.UNKNOWN, orders=self._orders, now=now
        )
        return ToolResult.error(
            ToolErrorCode.UNKNOWN_OUTCOME,
            "refund outcome unknown; retry with the same idempotency_key to resolve it",
            retryable=False,
        )
