"""Agent layer (Day 3).

LangGraph pipeline over the Day 2 tools: init -> classify -> gather ->
select_plan -> validate_plan, then approval pause / execution / result
verification / reply / finalize. LLM collaborators (classifier, plan
proposer, reply generator) sit behind interfaces with deterministic
defaults, so the graph runs and is tested without any model API.

Safety invariants live in :mod:`app.agent.graph`, the plan store
(:mod:`app.services.plans`) and the approval service
(:mod:`app.services.approvals`); refund execution stays inside the
Day 2 :class:`~app.services.refunds.RefundService` boundary.
"""
