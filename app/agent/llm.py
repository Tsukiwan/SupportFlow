"""LLM collaborators behind interfaces.

The graph never hands tools to a model and never treats model output as
authoritative: classification, planning and reply writing are advisory
proposals that the graph validates against trusted stores. Each
collaborator therefore sits behind a protocol with a *deterministic
default implementation* (no network, no API key) so the whole agent runs
offline; the DeepSeek-backed implementations are only constructed when an
API key is configured and are not exercised by the test suite (no real
API dependency).

Model output is untrusted input: JSON is parsed defensively and any
failure propagates to the caller, which retries a bounded number of times
and then falls back to the deterministic default — never to fabricated
state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.domain.enums import IssueType, RiskLevel, RISK_SEVERITY
from app.domain.models import (
    Order,
    ResolutionPlan,
    ResolutionPlanStep,
    Shipment,
    Ticket,
)
from app.services.refund_policy import RefundPolicy

logger = logging.getLogger(__name__)


# --- structured inputs/outputs shared by implementations ---


class ClassificationProposal(BaseModel):
    """Advisory classification of a ticket (trusted record always wins)."""

    model_config = ConfigDict(frozen=True)

    issue_type: IssueType | None = None
    risk_level: RiskLevel | None = None
    note: str = Field(default="", max_length=200)


class PlanContext(BaseModel):
    """Trusted facts handed to the plan proposer. Ticket *text* is
    included as untrusted input; every field derived from it is
    re-validated before a plan is registered."""

    model_config = ConfigDict(frozen=True)

    ticket: Ticket
    order: Order | None = None
    shipment: Shipment | None = None
    policy: RefundPolicy | None = None
    trusted_issue_type: IssueType
    trusted_risk_level: RiskLevel


class ReplyFacts(BaseModel):
    """Verified facts for the reply generator, plus mechanical checks the
    generated reply must pass (required tokens present, forbidden tokens
    absent). A reply that fails the checks is replaced by the server
    template — verified results always win over model prose."""

    model_config = ConfigDict(frozen=True)

    kind: str
    lines: tuple[str, ...]
    required_tokens: tuple[str, ...] = ()
    forbidden_tokens: tuple[str, ...] = ()
    template: str


# --- protocols ---


class TicketClassifier(Protocol):
    async def classify(self, ticket: Ticket) -> ClassificationProposal: ...


class PlanProposer(Protocol):
    async def propose(self, context: PlanContext) -> ResolutionPlan: ...


class ReplyGenerator(Protocol):
    async def generate(self, facts: ReplyFacts) -> str: ...


# --- deterministic defaults (no LLM, no network) ---


class DefaultClassifier:
    async def classify(self, ticket: Ticket) -> ClassificationProposal:
        return ClassificationProposal(
            issue_type=ticket.issue_type,
            risk_level=ticket.risk_level,
            note="default classifier (no LLM configured)",
        )


class DefaultPlanProposer:
    """Rule-based proposals: refund when the policy allows and the ticket
    is a refund-style claim, otherwise answer from trusted order facts."""

    async def propose(self, ctx: PlanContext) -> ResolutionPlan:
        ticket = ctx.ticket
        policy = ctx.policy
        refund_style = ticket.issue_type in (
            IssueType.REFUND_REQUEST,
            IssueType.DELIVERY_ISSUE,
            IssueType.DAMAGED_ITEM,
        )
        high = RISK_SEVERITY[ctx.trusted_risk_level] >= RISK_SEVERITY[RiskLevel.HIGH]
        if (
            refund_style
            and policy is not None
            and policy.allowed
            and ctx.order is not None
            and ticket.claimed_amount is not None
        ):
            return ResolutionPlan(
                plan_id="draft",
                ticket_id=ticket.ticket_id,
                order_id=ticket.order_id,
                issue_type=ticket.issue_type,
                action="refund",
                amount=ticket.claimed_amount,
                risk_level=ctx.trusted_risk_level,
                requires_approval=policy.requires_approval or high,
                steps=(
                    ResolutionPlanStep(action="issue refund", reason=policy.reason),
                ),
                created_at=ticket.updated_at,
            )
        status = ctx.order.status.value if ctx.order else "order not found"
        return ResolutionPlan(
            plan_id="draft",
            ticket_id=ticket.ticket_id,
            order_id=ticket.order_id if ctx.order else None,
            issue_type=ticket.issue_type,
            action="reply_only",
            risk_level=ctx.trusted_risk_level,
            requires_approval=high,
            steps=(
                ResolutionPlanStep(
                    action="answer customer",
                    reason=f"respond from trusted order status ({status})",
                ),
            ),
            created_at=ticket.updated_at,
        )


class DefaultReplyGenerator:
    async def generate(self, facts: ReplyFacts) -> str:
        return facts.template


# --- reply verification ---


def verify_reply(reply: str, facts: ReplyFacts) -> bool:
    """Mechanical consistency check between a generated reply and the
    verified facts it must reflect."""
    lowered = reply.lower()
    if any(token.lower() in lowered for token in facts.forbidden_tokens):
        return False
    if any(token.lower() not in lowered for token in facts.required_tokens):
        return False
    return True


# --- DeepSeek-backed implementations (used only when an API key exists) ---


class DeepSeekChat:
    """Minimal OpenAI-compatible chat client (stdlib only)."""

    def __init__(self, settings: Settings) -> None:
        if not settings.has_deepseek_key:
            raise ValueError("DeepSeekChat requires a configured API key")
        self._settings = settings

    async def complete_json(
        self, *, system: str, user: str, timeout: float = 30.0
    ) -> dict[str, Any]:
        def _call() -> dict[str, Any]:
            payload = {
                "model": self._settings.deepseek_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            }
            request = urllib.request.Request(
                f"{self._settings.deepseek_base_url.rstrip('/')}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": (
                        f"Bearer {self._settings.deepseek_api_key.get_secret_value()}"
                    ),
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]

        raw = await asyncio.to_thread(_call)
        return _parse_json_object(raw)


def _parse_json_object(raw: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating Markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


class DeepSeekClassifier:
    def __init__(self, chat: DeepSeekChat) -> None:
        self._chat = chat

    async def classify(self, ticket: Ticket) -> ClassificationProposal:
        raw = await self._chat.complete_json(
            system=(
                "Classify a customer-support ticket. Return JSON only: "
                '{"issue_type": "...", "risk_level": "...", "note": "..."}. '
                "issue_type is one of refund_request, order_inquiry, "
                "delivery_issue, damaged_item, other. risk_level is one of "
                "low, medium, high, critical."
            ),
            user=(
                f"ticket {ticket.ticket_id}: {ticket.subject}\n{ticket.description}"
            ),
        )
        proposal = ClassificationProposal(
            issue_type=raw.get("issue_type"),
            risk_level=raw.get("risk_level"),
            note=str(raw.get("note", ""))[:200],
        )
        if proposal.issue_type is None or proposal.risk_level is None:
            raise ValidationError("classification proposal missing issue_type or risk_level")
        return proposal


class DeepSeekPlanProposer:
    def __init__(self, chat: DeepSeekChat) -> None:
        self._chat = chat

    async def propose(self, ctx: PlanContext) -> ResolutionPlan:
        order = ctx.order
        raw = await self._chat.complete_json(
            system=(
                "Propose a resolution plan for a support ticket. Return "
                "JSON only: "
                '{"action": "refund|note|reply_only", "amount": "12.34"|null, '
                '"steps": [{"action": "...", "reason": "..."}], '
                '"risk_level": "low|medium|high|critical", '
                '"requires_approval": true|false}. '
                "The plan is a proposal; the server re-validates it."
            ),
            user=(
                f"ticket {ctx.ticket.ticket_id} ({ctx.ticket.subject}): "
                f"{ctx.ticket.description}\n"
                f"trusted issue_type={ctx.trusted_issue_type.value} "
                f"risk_level={ctx.trusted_risk_level.value}\n"
                f"order={order.model_dump_json() if order else 'missing'}\n"
                f"policy={ctx.policy.model_dump_json() if ctx.policy else 'missing'}"
            ),
        )
        steps = tuple(
            ResolutionPlanStep(action=str(s["action"]), reason=str(s["reason"]))
            for s in raw.get("steps", [])
        )
        amount = raw.get("amount")
        return ResolutionPlan(
            plan_id="draft",
            ticket_id=ctx.ticket.ticket_id,
            order_id=ctx.ticket.order_id if order is not None else None,
            issue_type=ctx.ticket.issue_type,
            action=str(raw.get("action", "reply_only")),
            amount=Decimal(str(amount)) if amount not in (None, "") else None,
            risk_level=RiskLevel(str(raw.get("risk_level", "low"))),
            requires_approval=bool(raw.get("requires_approval", False)),
            steps=steps,
            created_at=ctx.ticket.updated_at,
        )


class DeepSeekReplyGenerator:
    def __init__(self, chat: DeepSeekChat) -> None:
        self._chat = chat

    async def generate(self, facts: ReplyFacts) -> str:
        raw = await self._chat.complete_json(
            system=(
                "Write a customer-service reply using ONLY the verified "
                "facts given. Return JSON only: {\"reply\": \"...\"}."
            ),
            user="\n".join(facts.lines),
        )
        return str(raw.get("reply", facts.template))


def build_llm_collaborators(
    settings: Settings,
) -> tuple[TicketClassifier, PlanProposer, ReplyGenerator]:
    """DeepSeek-backed collaborators when a key is configured, otherwise
    the deterministic defaults."""
    if not settings.has_deepseek_key:
        return DefaultClassifier(), DefaultPlanProposer(), DefaultReplyGenerator()
    chat = DeepSeekChat(settings)
    return (
        DeepSeekClassifier(chat),
        DeepSeekPlanProposer(chat),
        DeepSeekReplyGenerator(chat),
    )
