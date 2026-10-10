"""Deterministic mock orders, tickets and shipments for development and tests.

Moved here from ``tests/fixtures/mock_data.py`` (Day 2): the tool layer must
reuse the same scenario data without importing from ``tests/``. The old
module still re-exports everything, so Day 1 import paths keep working.

Every value is a hard-coded constant — no randomness, no wall clock — so
regenerating the catalog always yields identical data. Scenario names are
the stable contract between tests and the mock tool backend:

=======================  ==================================================
scenario                 shape of the data
=======================  ==================================================
refundable               delivered order, nothing refunded yet
already_refunded         delivered order, fully refunded
cancelled                order cancelled before delivery
amount_mismatch          ticket claims more than the order total
delivered_not_received   logistics says delivered, customer disputes it
non_existent             ticket references an order id that does not exist
=======================  ==================================================

``non_existent`` deliberately has no order: ``SCENARIO_ORDERS`` contains no
entry for it and ``get_scenario`` returns ``None`` as the order.

Shipments exist only for ``refundable`` and ``delivered_not_received``
(both with delivery evidence); the other orders have no shipping record,
which is exactly what ``get_shipping_status`` reports as
``SHIPPING_NOT_FOUND``.

Regenerate and inspect::

    uv run python -m app.persistence.mock_catalog
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.domain.enums import (
    IssueType,
    OrderStatus,
    RiskLevel,
    ShippingStatus,
    TicketStatus,
)
from app.domain.models import Order, Shipment, Ticket

REFERENCE_TIME = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)

MISSING_ORDER_ID = "ORD-9999"


def order_refundable() -> Order:
    return Order(
        order_id="ORD-1001",
        customer_id="CUS-1001",
        status=OrderStatus.DELIVERED,
        currency="USD",
        total_amount=Decimal("199.99"),
        refunded_amount=Decimal("0"),
        created_at=REFERENCE_TIME,
        delivered_at=REFERENCE_TIME + timedelta(days=4),
    )


def ticket_refundable() -> Ticket:
    return Ticket(
        ticket_id="TKT-2001",
        order_id="ORD-1001",
        customer_id="CUS-1001",
        status=TicketStatus.OPEN,
        issue_type=IssueType.REFUND_REQUEST,
        risk_level=RiskLevel.LOW,
        subject="Request full refund for headphones",
        description="The headphones arrived but I changed my mind and want a full refund.",
        claimed_amount=Decimal("199.99"),
        created_at=REFERENCE_TIME + timedelta(days=5),
        updated_at=REFERENCE_TIME + timedelta(days=5),
    )


def order_already_refunded() -> Order:
    return Order(
        order_id="ORD-1002",
        customer_id="CUS-1002",
        status=OrderStatus.DELIVERED,
        currency="USD",
        total_amount=Decimal("89.50"),
        refunded_amount=Decimal("89.50"),
        created_at=REFERENCE_TIME,
        delivered_at=REFERENCE_TIME + timedelta(days=3),
    )


def ticket_already_refunded() -> Ticket:
    return Ticket(
        ticket_id="TKT-2002",
        order_id="ORD-1002",
        customer_id="CUS-1002",
        issue_type=IssueType.REFUND_REQUEST,
        risk_level=RiskLevel.LOW,
        subject="Second refund request",
        description="Customer asks for a refund on an order that was already refunded in full.",
        claimed_amount=Decimal("89.50"),
        created_at=REFERENCE_TIME + timedelta(days=10),
        updated_at=REFERENCE_TIME + timedelta(days=10),
    )


def order_cancelled() -> Order:
    return Order(
        order_id="ORD-1003",
        customer_id="CUS-1003",
        status=OrderStatus.CANCELLED,
        currency="USD",
        total_amount=Decimal("45.00"),
        refunded_amount=Decimal("0"),
        created_at=REFERENCE_TIME + timedelta(days=1),
    )


def ticket_cancelled() -> Ticket:
    return Ticket(
        ticket_id="TKT-2003",
        order_id="ORD-1003",
        customer_id="CUS-1003",
        issue_type=IssueType.REFUND_REQUEST,
        risk_level=RiskLevel.MEDIUM,
        subject="Refund for cancelled order",
        description="Customer wants a refund for an order that was cancelled before shipping.",
        claimed_amount=Decimal("45.00"),
        created_at=REFERENCE_TIME + timedelta(days=2),
        updated_at=REFERENCE_TIME + timedelta(days=2),
    )


def order_amount_mismatch() -> Order:
    return Order(
        order_id="ORD-1004",
        customer_id="CUS-1004",
        status=OrderStatus.DELIVERED,
        currency="USD",
        total_amount=Decimal("299.00"),
        refunded_amount=Decimal("0"),
        created_at=REFERENCE_TIME,
        delivered_at=REFERENCE_TIME + timedelta(days=5),
    )


def ticket_amount_mismatch() -> Ticket:
    return Ticket(
        ticket_id="TKT-2004",
        order_id="ORD-1004",
        customer_id="CUS-1004",
        issue_type=IssueType.REFUND_REQUEST,
        risk_level=RiskLevel.MEDIUM,
        subject="Refund amount does not match",
        description="Customer claims 999.00 although the order total is only 299.00.",
        claimed_amount=Decimal("999.00"),
        created_at=REFERENCE_TIME + timedelta(days=6),
        updated_at=REFERENCE_TIME + timedelta(days=6),
    )


def order_delivered_not_received() -> Order:
    return Order(
        order_id="ORD-1005",
        customer_id="CUS-1005",
        status=OrderStatus.DELIVERED,
        currency="USD",
        total_amount=Decimal("129.90"),
        refunded_amount=Decimal("0"),
        created_at=REFERENCE_TIME,
        delivered_at=REFERENCE_TIME + timedelta(days=4, hours=6),
    )


def ticket_delivered_not_received() -> Ticket:
    return Ticket(
        ticket_id="TKT-2005",
        order_id="ORD-1005",
        customer_id="CUS-1005",
        issue_type=IssueType.DELIVERY_ISSUE,
        risk_level=RiskLevel.HIGH,
        subject="Parcel marked delivered but never arrived",
        description="Tracking shows delivered but the customer insists nothing was received.",
        claimed_amount=Decimal("129.90"),
        created_at=REFERENCE_TIME + timedelta(days=7),
        updated_at=REFERENCE_TIME + timedelta(days=7),
    )


def ticket_non_existent() -> Ticket:
    return Ticket(
        ticket_id="TKT-2006",
        order_id=MISSING_ORDER_ID,
        customer_id="CUS-1006",
        issue_type=IssueType.ORDER_INQUIRY,
        risk_level=RiskLevel.LOW,
        subject="Where is my order",
        description="Customer asks about an order id that does not exist in the system.",
        created_at=REFERENCE_TIME + timedelta(days=8),
        updated_at=REFERENCE_TIME + timedelta(days=8),
    )


def shipment_refundable() -> Shipment:
    return Shipment(
        order_id="ORD-1001",
        carrier="MockExpress",
        tracking_number="TRK-3001",
        status=ShippingStatus.DELIVERED,
        status_updated_at=REFERENCE_TIME + timedelta(days=4, hours=2),
        evidence=("delivery_photo",),
    )


def shipment_delivered_not_received() -> Shipment:
    return Shipment(
        order_id="ORD-1005",
        carrier="MockExpress",
        tracking_number="TRK-3005",
        status=ShippingStatus.DELIVERED,
        status_updated_at=REFERENCE_TIME + timedelta(days=4, hours=8),
        evidence=("signature_confirmed", "gps_pin_match"),
    )


SCENARIO_NAMES = (
    "refundable",
    "already_refunded",
    "cancelled",
    "amount_mismatch",
    "delivered_not_received",
    "non_existent",
)


def build_scenario_orders() -> dict[str, Order]:
    """Regenerate the order catalog. Always returns equal data."""
    return {
        "refundable": order_refundable(),
        "already_refunded": order_already_refunded(),
        "cancelled": order_cancelled(),
        "amount_mismatch": order_amount_mismatch(),
        "delivered_not_received": order_delivered_not_received(),
        # "non_existent" intentionally has no order
    }


def build_scenario_tickets() -> dict[str, Ticket]:
    """Regenerate the ticket catalog. Always returns equal data."""
    return {
        "refundable": ticket_refundable(),
        "already_refunded": ticket_already_refunded(),
        "cancelled": ticket_cancelled(),
        "amount_mismatch": ticket_amount_mismatch(),
        "delivered_not_received": ticket_delivered_not_received(),
        "non_existent": ticket_non_existent(),
    }


def build_scenario_shipments() -> dict[str, Shipment]:
    """Regenerate the shipment catalog. Always returns equal data.

    Orders without an entry here have no shipping record at all.
    """
    return {
        "refundable": shipment_refundable(),
        "delivered_not_received": shipment_delivered_not_received(),
    }


SCENARIO_ORDERS: dict[str, Order] = build_scenario_orders()
SCENARIO_TICKETS: dict[str, Ticket] = build_scenario_tickets()
SCENARIO_SHIPMENTS: dict[str, Shipment] = build_scenario_shipments()


def build_orders_by_id() -> dict[str, Order]:
    """Order catalog keyed by ``order_id`` (what the stores need)."""
    return {order.order_id: order for order in build_scenario_orders().values()}


def build_tickets_by_id() -> dict[str, Ticket]:
    """Ticket catalog keyed by ``ticket_id`` (what the stores need)."""
    return {ticket.ticket_id: ticket for ticket in build_scenario_tickets().values()}


def build_shipments_by_order_id() -> dict[str, Shipment]:
    """Shipment catalog keyed by ``order_id`` (what the stores need)."""
    return {
        shipment.order_id: shipment
        for shipment in build_scenario_shipments().values()
    }


def get_scenario(name: str) -> tuple[Ticket, Order | None]:
    """Return ``(ticket, order)`` for a scenario.

    ``order`` is ``None`` when the order does not exist.
    """
    ticket = SCENARIO_TICKETS.get(name)
    if ticket is None:
        raise KeyError(f"unknown scenario: {name!r}")
    return ticket, SCENARIO_ORDERS.get(name)


def print_catalog() -> None:
    """Print the scenario summary table (stable output)."""
    for name in SCENARIO_NAMES:
        ticket, order = get_scenario(name)
        order_status = order.status.value if order else "<missing>"
        claimed = ticket.claimed_amount if ticket.claimed_amount is not None else "-"
        print(
            f"{name:<24} order={ticket.order_id:<10} status={order_status:<10} "
            f"ticket={ticket.ticket_id:<10} issue={ticket.issue_type.value:<16} "
            f"claimed={claimed}"
        )


if __name__ == "__main__":
    print_catalog()
