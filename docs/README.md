# Documentation map

## Current sources of truth

- [交付与资源规划](plans/delivery-and-resources-2026-09-08.md)：本轮基础 CI/PR 范围、平台限制及后续资源/凭据/UI/eval/CD；[任务记录](tasks/2026-09-08-github-ci.md)。

- [开发工具说明](development.md)：环境准备、诊断、检查和验证证据；[本批任务记录](tasks/2026-09-08-dev-environment.md)。

- [任务记录](tasks/README.md)：具体任务的执行进展与交接入口；使用约定由 AGENTS.md 维护，本目录提供已确认的中文模板与实验任务填写说明。

- [M0 execution plan](plans/m0-validation-plan-2026-09-07.md): reviewed P2 experiment scope, upgrade/recovery and eval rules, resource planning, evidence and implementation handoff. [Isolated-context review](reviews/m0-plan-adversarial-review-2026-09-07.md) records the two closed omissions; experiments have not run.

- [Approved C3 technical plan](design/technical-proposal-2026-09-07.md): current stack direction, recovery/control contracts, release observations, model adaptation, UI, eval, resource candidates and M0–M3. User-reviewed and authorized for persistence; [whole-design review](reviews/technical-design-c3-review-2026-09-07.md) records C1–C3 findings. Compatibility and runtime acceptance remain unproven.
- [ADR-0003](adr/0003-business-state-recovery-authority.md): why committed business records own cross-process recovery and graph checkpoints do not.
- `../SPEC.md`: what the product includes/excludes, full lifecycle requirements, status and design questions. Product scope and C3 design are approved; M0 evidence and acceptance calibration remain.
- [ADR-0002](adr/0002-context-driven-investigation.md): accepted shared investigation mechanism, context/knowledge approach and bounded execution; the later C3 plan records concrete technology direction without claiming runtime validation.
- `adr/0001-readonly-investigation-boundary.md`: why the consequential scope/authority decision was made. ADRs preserve trade-offs; they do not replace the specification.
- `../PRD.md`: user-facing capabilities with stable feature IDs; exact checks live only in `../feature_list.json`.
- `../feature_list.json`: machine-readable acceptance inventory. All passes remain false. Freeze the revised steps after technical review; do not change an approved check to conceal failure.
- `../ROADMAP.md`: order, decision gates and work status; it is not another feature specification.
- `../CONTEXT.md`: glossary only.
- [AGENTS.md](../AGENTS.md): sole shared project instruction source; [CLAUDE.md](../CLAUDE.md) imports it. `.Codex/rules/` holds compatibility pointers only. See [instruction migration](agents/instruction-migration-2026-09-08.md) for preserved rules and loading verification.

Use explicit Markdown links from the repository entry point. If current sources disagree, resolve the inconsistency before implementation; historical drafts cannot override current SPEC. User instructions remain authoritative.

## Evidence and historical material

- [Eval platform joint selection](research/eval-platform-selection-2026-09-07.md): LangSmith/Langfuse/LangWatch/Braintrust fit, local scenario/oracle responsibilities, deployment and cost boundaries; historical selection evidence; the approved C3 plan now selects LangSmith, with runtime/price validation pending.
- [Runtime selection recommendation](research/runtime-selection-2026-09-07.md): Pi/LangGraph/Agents SDK/manual-loop comparison with versioned evidence, application responsibilities and validation gates; historical comparison; current role boundaries and selection status live in C3.
- [2026-09-07 source-audit comparison](research/investigation-source-comparison-2026-09-07.md): entry-to-execution-to-storage traces for HolmesGPT/OpenSRE/Stratus, test coverage limits and unresolved behavior; read this before discussing concrete context/tool/state architecture.
- [Earlier proposal review](reviews/technical-proposal-review-2026-09-07.md): superseded checkpoint proposal and its bounded review, not the C3 approval record.
- [Outer product readiness audit](reviews/outer-product-readiness-2026-09-07.md): settled decisions versus remaining delivery, success and milestone definitions before detailed implementation planning.
- [Investigation design basis](research/investigation-design-basis-2026-09-06.md): cross-project fixed-source mechanisms, our trade-offs and validation obligations. [OpenSRE source trace](research/opensre-investigation-source-2026-09-06.md) supplies the detailed current entry path.
- [Initial evaluation coverage](testing/initial-investigation-coverage.md): test symptoms and counterexamples only; never runtime product categories.
- [Version-plan adversarial review](reviews/version-plan-adversarial-review-2026-09-06.md): independent review and disposition of the withdrawn conversational version proposal; not a new implementation plan.
- [Upstream-led plan](research/upstream-led-project-plan-2026-09-06.md): pinned source observations and concrete issue candidates; not runtime proof.
- [Secondary comparisons](research/upstream-secondary-benchmarks-2026-09-06.md): OpenSRE/kagent/K8sGPT references.
- Other files in `research/` and `research-basis.md`: dated supporting evidence and candidate tooling/environments, never implementation authority.
- `job-search-brainstorm-2026-09-06.md` and `complete-project-brainstorm-2026-09-06.md`: superseded discussion history; settled conclusions are in SPEC and ADR.
- [Original draft archive](archive/pre-readonly-scope-2026-09-06/README-ARCHIVE.md): byte-preserved prior specification/acceptance and stable-ID migration. Nested AGENTS/rules are historical copies only.
- `../issues/0001-initial-production-ops-agent-spec.md`: local tracking pointer to current documents, not a second copy of requirements.
- `../Codex-progress.txt`: local ignored session log, not the only place durable decisions are recorded. Tracked sources above carry shared decisions.

## Maintenance

Update SPEC when scope or constraints change; add a small ADR only for a consequential trade-off. Keep PRD and acceptance IDs aligned, update roadmap when work moves, and append the local progress log. Preserve retired IDs and old baselines when scope changes; never mark removed work complete. Research is version/date-bound: verify current source and actual behavior before adopting claims.

Do not create another plan or ADR for every conversation. Detailed component designs and execution plans should be created only when the capability map and runtime evidence justify them. No documentation framework or wiki service is needed at this stage.

For each material design choice, provide the concrete problem, a primary source/version or explicit original analysis, alternatives/trade-offs and a verification plan. A preferred upstream is not the sole design authority. Keep uncertain proposals labeled; do not turn test labels into product requirements or source existence into performance proof.
