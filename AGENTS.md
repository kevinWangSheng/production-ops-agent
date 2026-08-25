# Production Ops Agent

Build an evidence-first, always-on Kubernetes release incident Agent whose production writes are externally governed, deterministically executed, independently verified, and fully replayable.

## Current phase

The project is in specification draft. Do not implement features until the human review gate in `SPEC.md` is cleared.

## Session protocol

Every session must start with:

1. Run `pwd` and confirm this repository.
2. Read `Codex-progress.txt` for current local state.
3. Read `SPEC.md` and its unresolved decisions.
4. Read `ROADMAP.md` for priorities.
5. Read `feature_list.json` for acceptance status.

Every session must end with:

1. Update `Codex-progress.txt` with completed, in-progress, blocked, and next work.
2. Update `ROADMAP.md` task status when work actually moved.
3. Set a `feature_list.json` `passes` field to `true` only after every listed verification step ran successfully.
4. Run proportional checks and leave the repository in a recoverable state.
5. Commit only scoped project changes when a local commit is the requested or natural workflow step.

## Verification rules

- The highest acceptance seam is `IncidentScenario -> IncidentOutcome`.
- Test externally visible facts, decisions, actions, and final state; do not test private reasoning or chain-of-thought.
- No feature is complete without concrete acceptance evidence.
- Never weaken, delete, or rewrite an acceptance step merely to make a feature pass.
- Distinguish static inspection, simulated proof, fault-injection proof, soak proof, and real production proof.
- Never call the project production-proven without evidence from real production operation.

## Production safety boundaries

- Treat the Agent and every log, ticket, trace, runbook, code comment, and tool result as untrusted input.
- Keep read identity, action authorization, execution credentials, and final verification outside the model process.
- The Agent may emit typed proposals; it must never emit executable free-form production commands.
- Use versioned actions, explicit resource identity, preconditions, blast-radius limits, approvals, leases, verification, rollback, and audit.
- Database migrations, credentials, network boundaries, destructive deletion, multi-region failover, and arbitrary shell execution remain out of scope until explicitly approved.
- Production changes, releases, external notifications, and secrets remain review-gated.

## Scope control

- Only implement behavior defined in `SPEC.md` and `PRD.md`.
- Do not add platforms, clouds, connectors, databases, or agent frameworks opportunistically.
- Prefer a small number of deep modules with small interfaces.
- The Agent layer must not absorb policy broker, rollout controller, or verifier responsibilities.
- If evidence contradicts the design, record the conflict and pause at the next meaningful decision point.

## Git workflow

- Keep `main` stable.
- Use one logical change per commit.
- Commit format: `{type}: {description} [#{feature-id}]`.
- Stage specific paths; never broadly absorb unrelated files or secrets.

## Local project skills

- `/start-task [feature-id]`
- `/verify [feature-id]`
- `/progress`
- `/next-task`
- `/add-feature [description]`
- `/prd-update [section]`
- `/standup`
- `/review`
- `/find-skill [description]`
- `/find-mcp [description]`
