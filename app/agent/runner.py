"""Agent wiring and entry point.

``build_agent_wiring`` assembles the production dependencies — the *same*
:class:`~app.tools.registry.ToolSet` used by the Day 2 tool layer, plus the
plan store, the approval service (human decision entry) and the run
bookkeeping — and ``run_ticket`` executes one bounded graph run for a
ticket. A resumed ticket is simply run again: the graph re-loads the
approval-bound plan and re-verifies the approval (status, validity window,
plan hash) before executing anything.

Cancellation is recorded as CANCELLED and re-raised; any other graph
failure is recorded as a FAILED run (logged + audited) and returned as an
outcome, mirroring how tools convert errors into results instead of
raising into callers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.agent.graph import SupportFlowAgent, build_agent_graph
from app.agent.llm import (
    PlanProposer,
    ReplyGenerator,
    TicketClassifier,
    build_llm_collaborators,
)
from app.agent.state import AgentState
from app.config import Settings, get_settings
from app.domain.enums import AgentRunStatus
from app.domain.models import Order, Shipment, Ticket
from app.persistence.memory_stores import AgentRunStore, AuditStore
from app.services.approvals import ApprovalService
from app.services.audit import AuditRecorder
from app.services.payments import PaymentGateway, SimulatedPaymentGateway
from app.services.plans import PlanStore
from app.tools.context import ToolContext
from app.tools.registry import ToolSet, build_default_tool_set

logger = logging.getLogger(__name__)


class AgentDependencies:
    """Everything one graph run needs. Stores are the same instances the
    tool layer uses — the agent never builds a parallel registry."""

    def __init__(
        self,
        *,
        tools: ToolSet,
        plans: PlanStore,
        approvals: ApprovalService,
        runs: AgentRunStore,
        recorder: AuditRecorder,
        classifier: TicketClassifier,
        planner: PlanProposer,
        reply_generator: ReplyGenerator,
        llm_timeout: float = 30.0,
        clock=None,
    ) -> None:
        self.tools = tools
        self.plans = plans
        self.approvals = approvals
        self.runs = runs
        self.recorder = recorder
        self.classifier = classifier
        self.planner = planner
        self.reply_generator = reply_generator
        self.llm_timeout = llm_timeout
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def graph(self) -> Any:
        return build_agent_graph(
            SupportFlowAgent(
                tools=self.tools,
                plans=self.plans,
                approvals=self.approvals,
                runs=self.runs,
                recorder=self.recorder,
                classifier=self.classifier,
                planner=self.planner,
                reply_generator=self.reply_generator,
                llm_timeout=self.llm_timeout,
                clock=self.clock,
            )
        )


class RunOutcome(BaseModel):
    """Result of one agent run (mirrors the final graph state)."""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=64)
    run_status: AgentRunStatus
    phase: str
    plan_id: str | None = None
    final_reply: str | None = None
    error: str | None = None
    iterations: int = 0


def build_agent_wiring(
    *,
    gateway: PaymentGateway | None = None,
    orders: dict[str, Order] | None = None,
    tickets: dict[str, Ticket] | None = None,
    shipments: dict[str, Shipment] | None = None,
    plans: PlanStore | None = None,
    settings: Settings | None = None,
    classifier: TicketClassifier | None = None,
    planner: PlanProposer | None = None,
    reply_generator: ReplyGenerator | None = None,
    llm_timeout: float = 30.0,
    clock=None,
) -> AgentDependencies:
    """Assemble dependencies over the deterministic mock catalog (or
    injected stores for tests). Uses the production ToolSet construction —
    the same registry the tool layer exposes."""
    settings = settings or get_settings()
    # One PlanStore shared by the graph and RefundService: approvals are
    # re-verified against the same registry the agent registers plans into.
    plan_store = plans or PlanStore()
    tools = (
        build_default_tool_set(gateway=gateway, plans=plan_store)
        if orders is None and tickets is None and shipments is None
        else _tool_set_with_injected_data(
            gateway=gateway, orders=orders, tickets=tickets,
            shipments=shipments, plans=plan_store,
        )
    )
    recorder = AuditRecorder(tools.audit_store)
    approval_service = ApprovalService(store=tools.approvals, recorder=recorder)
    llm_classifier = llm_planner = llm_reply = None
    if classifier is None or planner is None or reply_generator is None:
        # Only reach for the configured model when an override is missing.
        llm_classifier, llm_planner, llm_reply = build_llm_collaborators(settings)
    return AgentDependencies(
        tools=tools,
        plans=plan_store,
        approvals=approval_service,
        runs=AgentRunStore(),
        recorder=recorder,
        classifier=classifier or llm_classifier,
        planner=planner or llm_planner,
        reply_generator=reply_generator or llm_reply,
        llm_timeout=llm_timeout,
        clock=clock,
    )


def _tool_set_with_injected_data(
    *,
    gateway: PaymentGateway | None,
    orders: dict[str, Order] | None,
    tickets: dict[str, Ticket] | None,
    shipments: dict[str, Shipment] | None,
    plans: PlanStore | None,
) -> ToolSet:
    """Same ToolSet construction as build_default_tool_set, over injected
    store contents (test-only path; the catalog is the production one)."""
    from app.persistence.memory_stores import (
        ApprovalStore,
        OrderStore,
        RefundStore,
        ShippingStore,
        TicketNoteStore,
        TicketStore,
    )
    from app.persistence.mock_catalog import (
        build_orders_by_id,
        build_shipments_by_order_id,
        build_tickets_by_id,
    )

    recorder = AuditRecorder(AuditStore())
    return ToolSet(
        orders=OrderStore(orders if orders is not None else build_orders_by_id()),
        refunds=RefundStore(),
        shipments=ShippingStore(
            shipments if shipments is not None else build_shipments_by_order_id()
        ),
        tickets=TicketStore(tickets if tickets is not None else build_tickets_by_id()),
        notes=TicketNoteStore(),
        approvals=ApprovalStore(),
        gateway=gateway or SimulatedPaymentGateway(),
        recorder=recorder,
        plans=plans,
    )


async def run_ticket(
    ticket_id: str,
    deps: AgentDependencies,
    *,
    max_iterations: int | None = None,
    run_id: str | None = None,
) -> RunOutcome:
    """Run the agent graph once for a ticket. ``run_id`` is traceability
    metadata only: cross-run retries still go through the idempotency and
    approval gates in the service layer."""
    max_iterations = max_iterations or 10
    run_id = run_id or f"RUN-{uuid4().hex[:12]}"
    graph = deps.graph()
    initial: AgentState = {
        "ticket_id": ticket_id,
        "run_id": run_id,
        "max_iterations": max_iterations,
        "iterations": 0,
        "started_at": deps.clock(),
    }
    # LangGraph's own recursion guard sits above our bounded loop.
    config = {"recursion_limit": max_iterations * 2 + 16}
    try:
        final = await graph.ainvoke(initial, config=config)
    except asyncio.CancelledError:
        await _finalize_cancelled(deps, ticket_id, run_id)
        raise
    except Exception as exc:
        logger.exception("agent run %s failed unexpectedly", run_id)
        await _finalize_failed(deps, ticket_id, run_id, str(exc))
        return RunOutcome(
            run_id=run_id,
            ticket_id=ticket_id,
            run_status=AgentRunStatus.FAILED,
            phase="failed",
            error=f"unexpected graph failure: {exc}",
        )
    return RunOutcome(
        run_id=run_id,
        ticket_id=ticket_id,
        run_status=final.get("run_status", AgentRunStatus.FAILED),
        phase=final.get("phase", "failed"),
        plan_id=final.get("plan_id"),
        final_reply=final.get("final_reply"),
        error=final.get("error"),
        iterations=final.get("iterations", 0),
    )


async def _finalize_cancelled(
    deps: AgentDependencies, ticket_id: str, run_id: str
) -> None:
    now = deps.clock()
    run = deps.runs.get(run_id)
    if run is not None:
        deps.runs.update(
            run_id,
            status=AgentRunStatus.CANCELLED,
            finished_at=now,
        )
    await deps.recorder.record(
        tool="agent_run_finished",
        ctx=ToolContext(ticket_id=ticket_id, run_id=run_id),
        outcome="cancelled",
        details={"run_status": "cancelled"},
    )


async def _finalize_failed(
    deps: AgentDependencies, ticket_id: str, run_id: str, error: str
) -> None:
    now = deps.clock()
    run = deps.runs.get(run_id)
    if run is not None:
        deps.runs.update(
            run_id,
            status=AgentRunStatus.FAILED,
            error=error,
            finished_at=now,
        )
    await deps.recorder.record(
        tool="agent_run_finished",
        ctx=ToolContext(ticket_id=ticket_id, run_id=run_id),
        outcome="execution_failure",
        details={"run_status": "failed", "error": error[:200]},
    )
