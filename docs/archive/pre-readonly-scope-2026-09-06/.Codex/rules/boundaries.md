# Behavioral Boundaries

## Scope

- Implement only approved features in `SPEC.md` and `PRD.md`.
- Work on one feature at a time.
- Do not add clouds, databases, connectors, frameworks, or user interfaces opportunistically.
- Do not replace Temporal, OPA, Argo Rollouts, or the observability stack without a reviewed design decision once they are adopted.

## Production Authority

- The model process is untrusted and read-only.
- Free-form production commands are prohibited.
- Every mutation must use a versioned action contract and external broker.
- Missing target, policy, approval, identity, lease, or verification data fails closed.
- Database, credential, IAM, network, destructive deletion, multi-region, and arbitrary-shell actions are out of scope.
- Production changes and external notifications remain review-gated.

## Communication

- Separate checked facts, inferences, recommendations, and unverified claims.
- When requirements materially affect architecture or risk, stop at the human review gate.
- Document newly discovered issues without silently expanding scope.

## File Integrity

- Do not delete or restructure management files without explicit approval.
- Do not modify `.Codex/rules/` unless explicitly asked.
- Once a feature is approved, do not modify its `feature_list.json` verification steps merely to accommodate an implementation.
