"""Audit trail: every tool call leaves a classified, secret-free event."""

import asyncio
from decimal import Decimal

from app.domain.enums import IssueType
from app.services.payments import GatewayOutcome
from app.tools.result import ToolErrorCode
from tests.tools_util import build_tool_set, ctx


def run(coro):
    return asyncio.run(coro)


def test_every_tool_call_leaves_an_audit_event():
    ts = build_tool_set()
    run(ts.get_order(ctx(ticket_id="TKT-2001", run_id="RUN-42"), order_id="ORD-1001"))
    run(ts.get_order(ctx(ticket_id="TKT-2001", run_id="RUN-42"), order_id="ORD-9999"))
    events = ts.audit_store.all()
    assert len(events) == 2
    assert [e.action for e in events] == ["get_order", "get_order"]
    ok_event, fail_event = events
    assert ok_event.details["outcome"] == "success"
    assert ok_event.details["duration_ms"] >= 0
    assert ok_event.details["order_id"] == "ORD-1001"
    assert ok_event.ticket_id == "TKT-2001"
    assert fail_event.details["outcome"] == "business_rejection"
    assert fail_event.details["error_code"] == ToolErrorCode.ORDER_NOT_FOUND.value


def test_create_refund_success_records_refund_id():
    ts = build_tool_set()
    result = run(ts.create_refund(ctx=ctx(), order_id="ORD-1001",
                                  amount=Decimal("50.00"),
                                  issue_type=IssueType.REFUND_REQUEST,
                                  idempotency_key="idem-audit-0001"))
    assert result.success is True
    events = [e for e in ts.audit_store.all() if e.action == "create_refund"]
    assert len(events) == 1
    assert events[0].details["outcome"] == "success"
    assert events[0].details["refund_id"] == result.data.refund_id
    assert events[0].details["order_id"] == "ORD-1001"


def test_invalid_input_audit_category():
    ts = build_tool_set()
    run(ts.get_order(ctx(), order_id=""))
    events = ts.audit_store.all()
    assert len(events) == 1
    assert events[0].details["outcome"] == "invalid_input"
    assert events[0].details["error_code"] == ToolErrorCode.INVALID_INPUT.value


def test_transient_failure_audit_category():
    ts = build_tool_set(gateway_outcomes=[GatewayOutcome.TRANSIENT_FAILURE])
    run(ts.create_refund(ctx=ctx(), order_id="ORD-1001", amount=Decimal("50.00"),
                         issue_type=IssueType.REFUND_REQUEST,
                         idempotency_key="idem-audit-0002"))
    event = [e for e in ts.audit_store.all() if e.action == "create_refund"][0]
    assert event.details["outcome"] == "transient_failure"
    assert event.details["error_code"] == ToolErrorCode.UPSTREAM_TIMEOUT.value


def test_unknown_outcome_audit_category():
    ts = build_tool_set(gateway_outcomes=[GatewayOutcome.UNKNOWN])
    run(ts.create_refund(ctx=ctx(), order_id="ORD-1001", amount=Decimal("50.00"),
                         issue_type=IssueType.REFUND_REQUEST,
                         idempotency_key="idem-audit-0003"))
    event = [e for e in ts.audit_store.all() if e.action == "create_refund"][0]
    assert event.details["outcome"] == "unknown"
    assert event.details["error_code"] == ToolErrorCode.UNKNOWN_OUTCOME.value


class _BoomGateway:
    async def execute(self, refund):
        raise RuntimeError("boom")

    async def status(self, refund_id):
        raise RuntimeError("boom")


def test_gateway_exception_audit_category_is_unknown():
    """A gateway exception is an unknown outcome, not a definitive
    execution failure — the audit trail must say so."""
    ts = build_tool_set(gateway=_BoomGateway())
    result = run(ts.create_refund(ctx=ctx(), order_id="ORD-1001",
                                  amount=Decimal("50.00"),
                                  issue_type=IssueType.REFUND_REQUEST,
                                  idempotency_key="idem-audit-0004"))
    assert result.error_code is ToolErrorCode.UNKNOWN_OUTCOME
    event = [e for e in ts.audit_store.all() if e.action == "create_refund"][0]
    assert event.details["outcome"] == "unknown"
    assert event.details["error_code"] == ToolErrorCode.UNKNOWN_OUTCOME.value


def test_execution_failure_audit_category(monkeypatch):
    """Genuine internal failures (a store bug, not a gateway exception)
    are classified as execution_failure."""
    ts = build_tool_set()

    def explode(*args, **kwargs):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(ts.refund_store, "create", explode)
    result = run(ts.create_refund(ctx=ctx(), order_id="ORD-1001",
                                  amount=Decimal("50.00"),
                                  issue_type=IssueType.REFUND_REQUEST,
                                  idempotency_key="idem-audit-0007"))
    assert result.error_code is ToolErrorCode.INTERNAL_ERROR
    event = [e for e in ts.audit_store.all() if e.action == "create_refund"][0]
    assert event.details["outcome"] == "execution_failure"
    assert event.details["error_code"] == ToolErrorCode.INTERNAL_ERROR.value


def test_run_id_links_refund_and_audit_event():
    ts = build_tool_set()
    result = run(ts.create_refund(ctx=ctx(ticket_id="TKT-2001", run_id="RUN-42"),
                                  order_id="ORD-1001", amount=Decimal("50.00"),
                                  issue_type=IssueType.REFUND_REQUEST,
                                  idempotency_key="idem-run-0001"))
    assert result.success is True
    assert ts.refund_store.all()[0].run_id == "RUN-42"
    event = [e for e in ts.audit_store.all() if e.action == "create_refund"][0]
    assert event.run_id == "RUN-42"


def test_same_key_retry_from_another_run_resolves_not_conflicts():
    """run_id is traceability metadata, not part of the idempotency
    intent: a cross-run retry with the same key resolves the original
    record instead of conflicting."""
    ts = build_tool_set()
    args = dict(order_id="ORD-1001", amount=Decimal("50.00"),
                issue_type=IssueType.REFUND_REQUEST,
                idempotency_key="idem-run-0002")
    first = run(ts.create_refund(ctx=ctx(ticket_id="TKT-2001", run_id="RUN-1"), **args))
    second = run(ts.create_refund(ctx=ctx(ticket_id="TKT-2001", run_id="RUN-2"), **args))
    assert first.success and second.success
    assert second.data.refund_id == first.data.refund_id
    assert ts.refund_store.count() == 1


def test_audit_events_carry_no_secret_material():
    ts = build_tool_set()
    run(ts.get_order(ctx(), order_id="ORD-1001"))
    run(ts.create_refund(ctx=ctx(), order_id="ORD-1001", amount=Decimal("50.00"),
                         issue_type=IssueType.REFUND_REQUEST,
                         idempotency_key="idem-audit-0005"))
    for event in ts.audit_store.all():
        payload = event.model_dump_json()
        assert "sk-" not in payload
        assert "api_key" not in payload
        assert "password" not in payload


def test_tool_failure_does_not_resolve_or_close_ticket():
    ts = build_tool_set(gateway_outcomes=[GatewayOutcome.TRANSIENT_FAILURE])
    result = run(ts.create_refund(ctx=ctx(), order_id="ORD-1001",
                                  amount=Decimal("50.00"),
                                  issue_type=IssueType.REFUND_REQUEST,
                                  idempotency_key="idem-audit-0006"))
    assert result.success is False
    # ticket status is untouched by the tool layer
    ticket = ts.tickets.get("TKT-2001")
    assert ticket.status.value == "open"
