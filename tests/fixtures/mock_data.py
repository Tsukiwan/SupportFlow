"""Backward-compatible re-export of the deterministic mock catalog.

The canonical catalog moved to ``app.persistence.mock_catalog`` (Day 2) so
the tool layer can reuse the scenario data without importing from
``tests/``. Day 1 tests keep their import paths unchanged.
"""

from app.persistence.mock_catalog import (
    MISSING_ORDER_ID,
    REFERENCE_TIME,
    SCENARIO_NAMES,
    SCENARIO_ORDERS,
    SCENARIO_TICKETS,
    build_scenario_orders,
    build_scenario_tickets,
    get_scenario,
    order_already_refunded,
    order_amount_mismatch,
    order_cancelled,
    order_delivered_not_received,
    order_refundable,
    print_catalog,
    ticket_already_refunded,
    ticket_amount_mismatch,
    ticket_cancelled,
    ticket_delivered_not_received,
    ticket_non_existent,
    ticket_refundable,
)

__all__ = [name for name in globals() if not name.startswith("_")]

if __name__ == "__main__":
    print_catalog()
