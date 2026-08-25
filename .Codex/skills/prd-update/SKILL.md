---
name: prd-update
description: "Update an approved PRD section and synchronize affected tracking records."
disable-model-invocation: true
argument-hint: "[section or feature-id]"
---

# Update PRD

1. Read the full PRD, SPEC, roadmap, and affected feature inventory.
2. Identify whether the change alters risk, scope, interfaces, testing seam, or a previous decision.
3. Apply the requested revision to PRD and SPEC where applicable.
4. Synchronize description, priority, and verification steps in `feature_list.json` and ROADMAP.
5. Record why the decision changed and require renewed review if production authority expanded.
