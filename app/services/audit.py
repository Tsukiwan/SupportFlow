"""Audit trail recording for tool executions and key service decisions.

Events are :class:`~app.domain.models.AuditEvent` instances written to an
:class:`~app.persistence.memory_stores.AuditStore`. ``details`` may carry
outcome category, duration, error code and related ids — never secrets
(enforced by the AuditEvent validator).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from app.domain.models import AuditEvent
from app.persistence.memory_stores import AuditStore

if TYPE_CHECKING:
    # Imported for type hints only — importing it eagerly would create a
    # cycle (app.tools.registry -> app.services.refunds -> app.services.audit).
    from app.tools.context import ToolContext


class AuditRecorder:
    def __init__(self, store: AuditStore) -> None:
        self.store = store

    async def record(
        self,
        *,
        tool: str,
        ctx: ToolContext | None = None,
        outcome: str,
        duration_ms: float | None = None,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
        actor: str = "agent",
    ) -> None:
        extra: dict[str, Any] = dict(details or {})
        extra["outcome"] = outcome
        if duration_ms is not None:
            extra["duration_ms"] = round(duration_ms, 3)
        if error_code is not None:
            extra["error_code"] = error_code
        event = AuditEvent(
            event_id=f"EVT-{uuid4().hex[:12]}",
            timestamp=datetime.now(timezone.utc),
            actor=actor,
            action=tool,
            ticket_id=ctx.ticket_id if ctx else None,
            run_id=ctx.run_id if ctx else None,
            details=extra,
        )
        self.store.add(event)
