---
name: verify
description: "Verify one feature against its immutable acceptance inventory."
disable-model-invocation: true
argument-hint: "[feature-id, e.g. F1]"
---

# Verify Feature

1. Read the feature in `PRD.md` and `feature_list.json`.
2. Execute every listed step and retain concrete output or artifacts.
3. Label evidence as static, contract, integrated runtime, fault-injection, soak, or real production.
4. Report each step as PASS or FAIL without weakening the step.
5. Only if all pass, set `passes: true`, update ROADMAP and progress, and record the date.
6. If any fail, keep `passes: false` and document the exact failure.
