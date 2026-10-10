"""add_ticket_note: success, unknown ticket, invalid note."""

import asyncio

from app.tools.result import ToolErrorCode
from tests.tools_util import build_tool_set, ctx


def run(coro):
    return asyncio.run(coro)


def test_add_note_success():
    ts = build_tool_set()
    result = run(
        ts.add_ticket_note(
            ctx(), ticket_id="TKT-2001", note="Customer confirmed the refund request."
        )
    )
    assert result.success is True
    assert result.data.ticket_id == "TKT-2001"
    assert result.data.author == "agent"
    assert result.data.text == "Customer confirmed the refund request."
    assert len(ts.notes.list_for("TKT-2001")) == 1


def test_add_note_unknown_ticket():
    ts = build_tool_set()
    result = run(
        ts.add_ticket_note(ctx(), ticket_id="TKT-9999", note="This should fail.")
    )
    assert result.success is False
    assert result.error_code is ToolErrorCode.TICKET_NOT_FOUND
    assert ts.notes.list_for("TKT-9999") == ()


def test_add_note_empty_note_is_invalid_input():
    ts = build_tool_set()
    result = run(ts.add_ticket_note(ctx(), ticket_id="TKT-2001", note=""))
    assert result.error_code is ToolErrorCode.INVALID_INPUT
    assert ts.notes.list_for("TKT-2001") == ()


def test_multiple_notes_accumulate():
    ts = build_tool_set()
    run(ts.add_ticket_note(ctx(), ticket_id="TKT-2001", note="First agent note."))
    run(ts.add_ticket_note(ctx(), ticket_id="TKT-2001", note="Second agent note."))
    assert len(ts.notes.list_for("TKT-2001")) == 2
