"""Trusted plan state: immutable, content-hashed resolution plans.

The agent's LLM only *proposes* plans. What executes is what this store
holds: the server assigns the ``plan_id``, validates the content, and
records a SHA-256 hash of the full plan. Approvals bind to that hash, and
execution re-loads the plan from here — never from the LLM's words — so:

- a same-``plan_id`` re-registration with different content is rejected;
- mutating an in-memory copy of a plan cannot change what executes;
- an approval can never authorize content that differs from what the
  human reviewed (the hash breaks), even under a reused ``plan_id``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.models import ResolutionPlan


def plan_content_hash(plan: ResolutionPlan) -> str:
    """Deterministic SHA-256 over the canonical JSON of the full plan
    content (including ``plan_id``, so the id and the content are bound)."""
    canonical = json.dumps(
        plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StoredPlan(BaseModel):
    """A registered plan: content plus its integrity hash."""

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=64)
    content: ResolutionPlan
    content_hash: str = Field(min_length=16, max_length=128)
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    created_at: AwareDatetime


class PlanStore:
    """Immutable plan registry keyed by ``plan_id``.

    In-memory and single-process only: cross-process immutability needs a
    database-backed registry with the same hash contract (Day 4).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, StoredPlan] = {}

    def register(
        self,
        plan: ResolutionPlan,
        *,
        run_id: str | None,
        created_at: datetime,
    ) -> StoredPlan:
        """Register a plan. Re-registering the same id with *identical*
        content is idempotent; different content under a used id fails
        closed — a reused plan_id must never shadow an approved plan."""
        stored = StoredPlan(
            plan_id=plan.plan_id,
            ticket_id=plan.ticket_id,
            content=plan,
            content_hash=plan_content_hash(plan),
            run_id=run_id,
            created_at=created_at,
        )
        with self._lock:
            existing = self._by_id.get(plan.plan_id)
            if existing is not None and existing.content_hash != stored.content_hash:
                raise ValueError(
                    f"plan_id {plan.plan_id} is already registered with different content"
                )
            self._by_id[plan.plan_id] = stored
            return stored

    def get(self, plan_id: str) -> StoredPlan | None:
        with self._lock:
            return self._by_id.get(plan_id)

    def load_verified(self, plan_id: str) -> StoredPlan | None:
        """Load and re-verify: recompute the hash of the stored content and
        refuse to return the plan if it no longer matches (drift)."""
        with self._lock:
            stored = self._by_id.get(plan_id)
            if stored is None:
                return None
            if plan_content_hash(stored.content) != stored.content_hash:
                return None
            return stored

    def verify(self, plan_id: str) -> Literal["ok", "missing", "drift"]:
        with self._lock:
            stored = self._by_id.get(plan_id)
            if stored is None:
                return "missing"
            if plan_content_hash(stored.content) != stored.content_hash:
                return "drift"
            return "ok"

    def all(self) -> tuple[StoredPlan, ...]:
        with self._lock:
            return tuple(self._by_id.values())
