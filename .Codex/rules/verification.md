# Verification Rules

## Definition of Done

A feature is complete only when:

1. Its PRD acceptance criteria are met.
2. Every immutable verification step in `feature_list.json` has run and passed.
3. The result is supported by concrete output or an artifact.
4. The `passes` field is set to `true`.
5. The ROADMAP entry is moved to Completed with a date.
6. The project remains runnable and recoverable.

## Highest Acceptance Seam

Prefer the external `IncidentScenario -> IncidentOutcome` seam. Assert observable evidence, decisions, actions, audit, and final environment state. Do not assert chain-of-thought, private prompt structure, framework graph shape, or internal call order.

## Evidence Labels

Report evidence as one of:

- Static inspection.
- Unit or contract test.
- Integrated runtime test.
- Fault-injection test.
- Soak test.
- Real production observation.

Do not promote one evidence class into another.

## Prohibited Actions

- Never mark a feature complete without running its checks.
- Never delete or weaken a failing acceptance step to make it pass.
- Never substitute an LLM judge for deterministic safety or final-state assertions.
- Never hide failed scenarios from published results.
- Never claim production readiness from a local happy-path demo.
