"""Enum definitions, values and risk ordering."""

import pytest

from app.domain.enums import (
    IssueType,
    RiskLevel,
    RISK_SEVERITY,
    TicketStatus,
)


def test_ticket_status_values():
    assert TicketStatus.OPEN.value == "open"
    assert TicketStatus.IN_PROGRESS.value == "in_progress"
    assert TicketStatus.PENDING_APPROVAL.value == "pending_approval"
    assert TicketStatus.RESOLVED.value == "resolved"
    assert TicketStatus.CLOSED.value == "closed"
    assert TicketStatus.CANCELLED.value == "cancelled"


def test_issue_type_roundtrip_from_value():
    assert IssueType("refund_request") is IssueType.REFUND_REQUEST
    assert IssueType("delivery_issue") is IssueType.DELIVERY_ISSUE
    assert IssueType("damaged_item") is IssueType.DAMAGED_ITEM


def test_unknown_enum_value_raises():
    with pytest.raises(ValueError):
        IssueType("no_such_issue")


def test_risk_severity_ordering():
    assert RISK_SEVERITY[RiskLevel.LOW] < RISK_SEVERITY[RiskLevel.MEDIUM]
    assert RISK_SEVERITY[RiskLevel.MEDIUM] < RISK_SEVERITY[RiskLevel.HIGH]
    assert RISK_SEVERITY[RiskLevel.HIGH] < RISK_SEVERITY[RiskLevel.CRITICAL]
