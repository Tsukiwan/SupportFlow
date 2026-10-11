"""Repeatable crash-recovery demo for the Day 6 recovery service.

Two phases, run as two separate processes against the same PostgreSQL
demo database — the process boundary is what makes the crash real:

- ``crash`` — create a refund (PENDING row, reservation held), let the
  *fake gateway* record that it executed the refund successfully, and
  exit WITHOUT finalizing. The ledger file is the gateway's state,
  which survives the application's death; the database row stays
  PENDING exactly as a process that crashed between ``execute`` and
  ``finalize`` would leave it.
- ``recover`` — a fresh process (fresh wiring, fresh stores) loads the
  gateway ledger, runs the recovery service, and settles the refund
  from the gateway's evidence. A second recovery run proves the amount
  does not move again.

The gateway double (``FakeGateway``) is for development and demo only —
it is NOT a real payment gateway, and nothing here touches real money.

Run with a disposable demo database and the postgres backend:

    SUPPORTFLOW_STORE_BACKEND=postgres \
    SUPPORTFLOW_DATABASE_URL=postgresql+psycopg://supportflow:<password>@127.0.0.1:5432/supportflow_demo \
      uv run python -m app.recovery_demo --phase crash
    # ... the second process simulates the restart:
    SUPPORTFLOW_STORE_BACKEND=postgres \
    SUPPORTFLOW_DATABASE_URL=postgresql+psycopg://supportflow:<password>@127.0.0.1:5432/supportflow_demo \
      uv run python -m app.recovery_demo --phase recover
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.agent.llm import (
    DefaultClassifier,
    DefaultPlanProposer,
    DefaultReplyGenerator,
)
from app.agent.runner import build_agent_wiring
from app.config import get_settings
from app.domain.enums import IssueType, RefundStatus
from app.persistence.pg_refund_store import PgRefundStore
from app.services.payments import FakeGateway, GatewayOutcome
from app.services.recovery import RecoveryService

ORDER_ID = "ORD-1001"  # refundable catalog order: total 199.99
TICKET_ID = "TKT-2001"  # low-risk refund request ticket
AMOUNT = Decimal("50.00")
IDEMPOTENCY_KEY = "demo-recovery-key"
DEFAULT_LEDGER = "demo_gateway_ledger.json"


def _wiring():
    settings = get_settings()
    if settings.store_backend != "postgres":
        print(
            "recovery demo requires SUPPORTFLOW_STORE_BACKEND=postgres and a "
            "postgresql+psycopg:// SUPPORTFLOW_DATABASE_URL (a disposable demo "
            "database); the recovery service only exists on the PostgreSQL "
            "backend because that is where state survives restarts",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return build_agent_wiring(
        settings=settings,
        gateway=FakeGateway(),
        classifier=DefaultClassifier(),
        planner=DefaultPlanProposer(),
        reply_generator=DefaultReplyGenerator(),
    )


def _read_ledger(path: Path) -> dict[str, GatewayOutcome]:
    raw = json.loads(path.read_text())
    return {rid: GatewayOutcome(value) for rid, value in raw.items()}


def _phase_crash(deps, ledger_path: Path) -> int:
    store: PgRefundStore = deps.tools.refund_store
    outcome = store.create(
        None,
        order_id=ORDER_ID,
        amount=AMOUNT,
        idempotency_key=IDEMPOTENCY_KEY,
        ticket_id=TICKET_ID,
        issue_type=IssueType.REFUND_REQUEST,
        plan_id=None,
        run_id=None,
        reason="recovery demo",
        ticket_claimed_amount=Decimal("199.99"),
        now=datetime.now(timezone.utc),
    )
    if outcome.kind != "execute":
        print(
            f"create returned kind={outcome.kind!r} — the demo database is not "
            "fresh (the idempotency key or the order already carries a "
            "refund). Drop and re-create the demo database to replay."
        )
        return 1
    refund_id = outcome.refund.refund_id
    # The gateway executes the refund and records success. The application
    # "crashes" right here — before finalize — so the row stays PENDING
    # with the reservation held. The ledger file stands in for the
    # gateway's own database, which survives the crash.
    ledger_path.write_text(json.dumps({refund_id: GatewayOutcome.PROCESSED.value}))
    print(f"phase crash: refund {refund_id} created (PENDING, reserved {AMOUNT})")
    print(f"  gateway executed successfully; application crashed before finalize")
    print(f"  gateway ledger written to {ledger_path}")
    return 0


def _phase_recover(deps, ledger_path: Path) -> int:
    if not ledger_path.exists():
        print(f"ledger file {ledger_path} not found — run --phase crash first")
        return 1
    store: PgRefundStore = deps.tools.refund_store
    gateway = FakeGateway(ledger=_read_ledger(ledger_path))
    service = RecoveryService(
        store=store, gateway=gateway, lease_seconds=5.0, pending_grace_seconds=0.0
    )

    first = asyncio.run(service.recover_once())
    print(f"recovery run 1: discovered={first.discovered} "
          f"settled_processed={first.settled_processed} "
          f"settled_failed={first.settled_failed} "
          f"kept_unknown={first.kept_unknown}")
    for result in first.results:
        print(f"  {result.refund_id}: {result.outcome.value} "
              f"(gateway {result.gateway_result.value if result.gateway_result else '-'})")

    refund_id = next(iter(_read_ledger(ledger_path)))
    refund = store.get(refund_id)
    assert refund is not None
    order = deps.tools.orders.get(ORDER_ID)
    print(f"  after recovery: status={refund.status.value} "
          f"refunded={order.refunded_amount} reserved={order.reserved_amount}")
    assert refund.status is RefundStatus.PROCESSED
    assert order.refunded_amount == AMOUNT and order.reserved_amount == Decimal("0")

    second = asyncio.run(service.recover_once())
    print(f"recovery run 2: discovered={second.discovered} "
          f"settled_processed={second.settled_processed} "
          f"kept_unknown={second.kept_unknown}")
    refund = store.get(refund_id)
    order = deps.tools.orders.get(ORDER_ID)
    print(f"  after second run: status={refund.status.value} "
          f"refunded={order.refunded_amount} reserved={order.reserved_amount}")
    assert order.refunded_amount == AMOUNT  # the money did not move twice

    print("\nAudit events for the refund:")
    for event in deps.tools.audit_store.all():
        if event.details.get("refund_id") == refund_id:
            print(f"  {event.action:<32} {event.details}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("crash", "recover"), required=True)
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    args = parser.parse_args()

    deps = _wiring()
    if args.phase == "crash":
        code = _phase_crash(deps, Path(args.ledger))
    else:
        code = _phase_recover(deps, Path(args.ledger))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
