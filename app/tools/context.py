"""Call context carried through tool invocations.

Supplies the ticket/run linkage used by audit events and the refund
approval gate. ``ticket_id`` is mandatory for ``create_refund`` — a refund
without ticket provenance is rejected up front.
"""

from pydantic import BaseModel, ConfigDict, Field


class ToolContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticket_id: str | None = Field(default=None, min_length=1, max_length=64)
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
