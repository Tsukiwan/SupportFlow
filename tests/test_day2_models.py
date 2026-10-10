"""Day 2 additions to the domain layer: refund idempotency key, approval
binding, UNKNOWN refund status, Shipment and TicketNote."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.enums import IssueType, RefundStatus, ShippingStatus
from app.domain.models import (
    Approval,
    Refund,
    Shipment,
    TicketNote,
)

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def make_refund(**overrides) -> dict:
    fields = dict(
        refund_id="REF-1",
        idempotency_key="idem-test-0001",
        ticket_id="TKT-1",
        order_id="ORD-1",
        amount=Decimal("10.00"),
        currency="USD",
        issue_type=IssueType.REFUND_REQUEST,
        reason="Refund requested by the customer.",
        created_at=T0,
    )
    fields.update(overrides)
    return fields


def test_refund_requires_idempotency_key():
    with pytest.raises(ValidationError):
        Refund(**make_refund(idempotency_key=None))


def test_refund_supports_unknown_status():
    refund = Refund(**make_refund(status=RefundStatus.UNKNOWN))
    assert refund.status is RefundStatus.UNKNOWN


def make_approval(**overrides) -> dict:
    fields = dict(
        approval_id="APR-1",
        ticket_id="TKT-1",
        order_id="ORD-1",
        plan_id="PLAN-1",
        action="refund",
        amount=Decimal("10.00"),
        risk_level="high",
        justification="High-value refund requires a human decision.",
        requested_at=T0,
    )
    fields.update(overrides)
    return fields


def test_approval_requires_order_binding():
    with pytest.raises(ValidationError):
        Approval(**make_approval(order_id=None))


def test_approval_requires_plan_version_binding():
    with pytest.raises(ValidationError):
        Approval(**make_approval(plan_id=None))


def test_shipment_model():
    shipment = Shipment(
        order_id="ORD-1",
        carrier="MockExpress",
        tracking_number="TRK-1",
        status=ShippingStatus.DELIVERED,
        status_updated_at=T0 + timedelta(days=1),
        evidence=("signature_confirmed",),
    )
    assert shipment.status is ShippingStatus.DELIVERED
    assert shipment.evidence == ("signature_confirmed",)


def test_ticket_note_length_limits():
    with pytest.raises(ValidationError):
        TicketNote(note_id="NOTE-1", ticket_id="TKT-1", text="", created_at=T0)
    with pytest.raises(ValidationError):
        TicketNote(
            note_id="NOTE-1", ticket_id="TKT-1", text="x" * 2001, created_at=T0
        )


def test_ticket_note_default_author():
    note = TicketNote(note_id="NOTE-1", ticket_id="TKT-1", text="ok", created_at=T0)
    assert note.author == "agent"
