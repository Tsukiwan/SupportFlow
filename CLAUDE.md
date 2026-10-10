# Engineering Rules

## General

- Prefer simple implementations over abstractions.
- Do not introduce frameworks unless required.
- Do not modify architecture without explaining why.
- Keep functions small and testable.
- Every new feature must include tests.
- Never silently swallow exceptions.
- Do not hardcode secrets.
- Keep external API calls behind interfaces.

## Development Workflow

Before implementing:
1. Inspect the existing code.
2. Identify relevant modules.
3. Explain the implementation plan briefly.
4. Implement the smallest viable change.
5. Run relevant tests.
6. Review the implementation for edge cases.

## Agent Rules

- All agent state must be explicit.
- Every tool call must be observable.
- Every tool must have timeout/error handling.
- Agent loops must have a hard iteration limit.
- Never allow unbounded tool execution.
- Preserve intermediate evidence.
- Separate planning, execution and synthesis.

## RAG Rules

- Retrieval logic must be independently testable.
- Keep retrieval and generation separate.
- Every retrieved document must contain source metadata.
- Evaluation datasets must be version controlled.
- Never claim retrieval quality without evaluation results.
