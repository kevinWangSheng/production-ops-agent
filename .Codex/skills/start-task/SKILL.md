---
name: start-task
description: "Start an approved feature from the PRD and acceptance inventory."
disable-model-invocation: true
argument-hint: "[feature-id, e.g. F1]"
---

# Start Task

1. Confirm the SPEC human-review gate is cleared for this feature.
2. Read the matching `PRD.md` and `feature_list.json` entries.
3. Present description, acceptance criteria, immutable verification steps, priority, dependencies, and safety boundaries.
4. Mark the ROADMAP entry `[-]` and log the start in `Codex-progress.txt`.
5. Inspect current code and ADRs, then implement only the approved scope.
