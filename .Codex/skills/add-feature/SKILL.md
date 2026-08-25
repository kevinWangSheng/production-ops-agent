---
name: add-feature
description: "Add an explicitly requested feature and synchronize product tracking."
disable-model-invocation: true
argument-hint: "[brief description]"
---

# Add Feature

1. Determine the next feature ID.
2. Capture name, actor, benefit, scope, priority, acceptance criteria, verification steps, dependencies, and production risks.
3. Update `SPEC.md` if the feature changes product scope or architecture.
4. Add the feature consistently to `PRD.md`, `feature_list.json`, and `ROADMAP.md`.
5. Keep `passes: false`; never mark newly specified work complete.
