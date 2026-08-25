# Session Protocol

## Session Start

1. Run `pwd` and confirm the Production Ops Agent repository.
2. Read `Codex-progress.txt`.
3. Read `SPEC.md`, including unresolved decisions and the implementation gate.
4. Read `ROADMAP.md`.
5. Read `feature_list.json`.
6. Report current verified feature count and the next approved task.

## Session End

1. Append completed, in-progress, blocked, and next work to `Codex-progress.txt`.
2. Update `ROADMAP.md` only when task state actually changed.
3. Set a feature's `passes` field only after every listed verification step passed.
4. Record concrete tests, runtime evidence, and remaining limitations.
5. Leave the repository runnable or clearly document why that is not yet applicable.

## Context Continuity

- Never begin implementation while the SPEC human-review gate remains closed.
- Never infer a production authorization from a prior design discussion.
- Recheck current checkout state, project instructions, ADRs, contracts, and evidence before changing a decision.
