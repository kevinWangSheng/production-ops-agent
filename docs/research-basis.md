# Research Basis

> 状态：历史讨论或研究证据，非当前规格。2026-09-06 已确认的产品边界以仓库根目录 SPEC.md 和 docs/adr/0001-readonly-investigation-boundary.md 为准；本文旧的候选、建议及待决表述不覆盖该共识。

This initial specification incorporates the following checked practices and risks from the 2026-08-24 market and engineering research snapshot.

## Production practices carried into the draft

- Progressively authorize production Agents instead of granting ambient human-like credentials.
- Route mutations through a deterministic actuation control plane with dry-run, concurrency checks, rate limits, interruptibility, post-action guardians, and global freeze controls.
- Keep the investigator probabilistic and the authority, executor, rollout state machine, and verifier deterministic.
- Evaluate recent, realistic incidents continuously and combine qualitative investigation review with deterministic action and final-state scoring.
- Treat logs, tickets, traces, runbooks, code, and other tool output as hostile input.
- Exercise noise, concurrent and correlated failures, metastable conditions, missing telemetry, wrong targets, and prompt injection.
- Preserve uncertainty, rejected hypotheses, evidence provenance, approval scope, actual execution, and verification as durable state.

## Primary references

- Google SRE, “AI in SRE: How Google is Engineering the Future of Reliable Operations” — progressive authorization, no ambient access, mandatory dry-run, Actuation Agent, post-actuation guardians, red button, and nightly evals.
- SREGym — live high-fidelity SRE scenarios with noise, concurrent/correlated failures, and metastable failures.
- Microsoft AIOpsLab — separate evaluation of detection, localization, diagnosis, and mitigation.
- Evidra Bench — action path and final-state verification.
- USENIX Security 2026 LogInject — passive prompt injection through logged attacker-controlled content.
- Deno Claw Patrol — external policy and credential enforcement for Agent access to production systems.

## Evidence boundary

These sources inform architecture and testing choices. They do not prove this project is production-ready, production-safe, or production-proven. Those claims require project-specific implementation and runtime evidence.
