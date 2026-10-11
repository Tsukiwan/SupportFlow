"""Deterministic end-to-end demo of the Day 3 agent.

No LLM, no network, no API key: the default classifier / plan proposer /
reply generator are rule-based, the gateway is the simulated one, and the
catalog scenarios are hard-coded — so the *behavior* of every run is
deterministic (ids and timestamps are the only varying output).

Run with::

    uv run python -m app.agent.demo
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.agent.llm import (
    DefaultClassifier,
    DefaultPlanProposer,
    DefaultReplyGenerator,
)
from app.agent.runner import build_agent_wiring, run_ticket
from app.config import get_settings
from app.domain.enums import ApprovalStatus


async def _demo() -> None:
    # Force the offline collaborators so the demo never touches the LLM
    # API even when an API key is configured.
    deps = build_agent_wiring(
        classifier=DefaultClassifier(),
        planner=DefaultPlanProposer(),
        reply_generator=DefaultReplyGenerator(),
    )
    pg_mode = get_settings().store_backend == "postgres"
    if pg_mode:
        print(
            "backend: postgres — the non_existent scenario is not run: "
            "its ticket binds an order that does not exist, which "
            "PostgreSQL referential integrity cannot represent (memory "
            "mode keeps that rejection path; see README).\n"
        )

    # 1. Low-risk refund request: full automatic pipeline.
    outcome = await run_ticket("TKT-2001", deps)
    _print("refundable (low-risk refund)", outcome)

    # 2. High-risk delivery dispute: pauses for approval, then a human
    #    decision is recorded through ApprovalService and a resumed run
    #    re-verifies the approval + plan hash before executing.
    first = await run_ticket("TKT-2005", deps)
    _print("delivered_not_received (first run)", first)
    approval = deps.approvals.latest_for("TKT-2005")
    assert approval is not None, "high-risk run must have requested approval"
    await deps.approvals.decide(
        approval.approval_id,
        status=ApprovalStatus.APPROVED,
        decided_by="alice",
        expires_at=deps.clock() + timedelta(days=7),
    )
    resumed = await run_ticket("TKT-2005", deps)
    _print("delivered_not_received (after APPROVED)", resumed)

    # 3. Claim exceeds the order total: refused by plan validation.
    outcome = await run_ticket("TKT-2004", deps)
    _print("amount_mismatch (claim > total)", outcome)

    # 4. Order does not exist: refused, no side effects. Not representable
    #    under PG referential integrity — memory mode only (see above).
    if not pg_mode:
        outcome = await run_ticket("TKT-2006", deps)
        _print("non_existent (order missing)", outcome)

    print("\nAudit trail (action, outcome, ticket, run):")
    for event in deps.tools.audit_store.all():
        outcome_label = event.details.get("outcome")
        print(
            f"  {event.action:<24} {str(outcome_label):<20} "
            f"{event.ticket_id or '-':<10} {event.run_id or '-'}"
        )


def _print(scenario: str, outcome) -> None:
    print(
        f"{scenario:<36} phase={outcome.phase:<18} "
        f"run={outcome.run_status.value:<18} "
        f"plan={outcome.plan_id or '-':<17} iterations={outcome.iterations}"
    )
    if outcome.final_reply:
        print(f"    reply: {outcome.final_reply}")
    if outcome.error:
        print(f"    error: {outcome.error}")


def main() -> None:
    asyncio.run(_demo())


if __name__ == "__main__":
    main()
