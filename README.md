# OpsPilot — Read-only Operations Investigator

A continuously running Agent for incident and post-release investigation. It gathers and challenges evidence, supports human handoff, observes recovery after human handling, and drafts reviewed postmortems. It does not execute production changes or control release gates.

## Status

Product boundary confirmed on 2026-09-06. C3 technical design is reviewed and saved; M0 compatibility/capacity validation and acceptance calibration are next. Implementation has not started. All 11 active feature entries are unpassed. The project targets production engineering standards; it is not production-proven.

HolmesGPT is the primary reference and preferred reuse candidate; OpenSRE is a product reference, K8sGPT a simpler comparison, and kagent an optional runtime reference. Baseline deployment and issue reproduction are still pending.

## Read first

1. [SPEC.md](SPEC.md) — current boundary, full lifecycle and remaining design work.
2. [Scope ADR](docs/adr/0001-readonly-investigation-boundary.md) — why production writes and release gates are excluded.
3. [PRD.md](PRD.md) and [acceptance inventory](feature_list.json) — user capabilities and checks.
4. [ROADMAP.md](ROADMAP.md) — current work and next design step.
5. [Documentation guide](docs/README.md) — sources of truth, research and history.

Next: follow the [approved technical plan](docs/design/technical-proposal-2026-09-07.md) into M0 validation and freeze the corresponding acceptance packet. Architecture approval is not runtime proof; feature entry conditions remain in SPEC.
