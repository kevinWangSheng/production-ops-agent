# Shared instruction migration — 2026-09-08

Status: user-authorized persistence after isolated-context adversarial plan review. This record explains migration; AGENTS.md alone owns current common instructions.

## Rule preservation and intentional changes

- boundaries.md: product exclusions, external authority, untrusted input, scoped work, evidence communication and acceptance integrity are in AGENTS sections Purpose and authority, Start and execute, Verify and report, and Changes, Git and handoff.
- git-workflow.md: commit types/format, specific staging, recoverable commits, stable main, feature branches, acceptance before merge and external/history authorization are retained in Changes, Git and handoff.
- verification.md: PRD plus every acceptance step, concrete artifacts, passes/date/status, recoverability, external seam, evidence classes, deterministic safety/final-state checks and failed-scenario disclosure are retained in Verify and report.
- session-protocol.md: directory/state checks, current work, acceptance and durable handoff remain. Mandatory full history/acceptance reading each session is replaced with first-takeover/full-scope SPEC review, a fixed cross-cutting reread floor for edit/experiment tasks, and task-specific deeper reading.
- The stale unselected-technology statement is retired because SPEC/C3 already approved selection. M0 evidence and the feature implementation gate remain unchanged.
- One feature at a time becomes one bounded active work item, accommodating authorized M0 experiments and engineering maintenance without inventing product features.
- Management-file deletion/restructuring still needs explicit authorization. The separate ban on editing .Codex/rules is superseded by this authorized migration; these four paths now only point to the shared contract.
- Read-only tasks do not write progress. Missing ignored local progress is recoverable from tracked project records.
- Project skills keep their existing paths and bytes. Names/path fallback replace unverified slash-command promises; host discovery and skill workflow adaptation remain separate work.

## Host contract and sources

CLAUDE.md contains only `@AGENTS.md`. Detailed sources use ordinary Markdown links; importing all project documents at startup is not intended. Sharing project content does not guarantee identical effective context across hosts with different global instructions and tools.

- [Claude project imports](https://code.claude.com/docs/en/memory#agents-md): official shared-file import behavior.
- [Codex instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md): official instruction chain.
- [OpenAI harness engineering](https://openai.com/index/harness-engineering/): concise entry, repository knowledge and feedback.
- [Thoughtworks harness guidance](https://martinfowler.com/articles/harness-engineering.html): project guides and verifiable feedback.
- [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f): knowledge indexing and maintenance; no parallel wiki or automatic authority over approved specifications is adopted.

## Verification

- Static checks passed: local links and all ten skill paths resolve; CLAUDE.md is exactly the single import; git diff --check passed. SPEC.md, PRD.md and feature_list.json remain byte-identical to the pre-edit snapshot. All 11 passes remain false.
- The original independent adversarial reviewer reread the persisted files and migration: no required correction; all three mandatory findings and both optional improvements covered. This is text/migration review, not product acceptance.
- Codex CLI 0.153.4 fresh-session probe exited 0 and correctly identified the fixed SPEC reread floor, absent-progress/read-only behavior, skill-path fallback and M0 gate. Probe used --ignore-user-config, --ephemeral, read-only sandbox and disabled shell tool; configured model retained. This establishes project instruction loading under a reduced configuration, not parity of all normal global hooks/plugins.
- Claude Code 2.1.263 probe retained normal CLAUDE discovery with tools/hooks/MCP disabled and no session persistence; exited 1 with: "You've hit your session limit · resets 2:20am (America/Los_Angeles)". No successful Claude model response was obtained; runtime import behavior remains unverified until account capacity returns.
- Local raw probe outputs: /tmp/opspilot-codex-loading.txt and /tmp/opspilot-claude-loading.txt. These temporary files are not portable or durable evidence; the observed outcomes and limitations are recorded here.
- Remaining: repeat the bounded Claude loading probe when available; later validate native skill discovery separately. No install, global-config edit, product implementation, M0 experiment, commit or publishing occurred.

## 中文与 worktree 约定补充 — 2026-09-08

用户确认公共项目指令以中文维护，并增加四条 worktree 生命周期约定。原公共约束逐条翻译，保留旧英文锚点供兼容指针使用；CLAUDE.md 仍只有 @AGENTS.md。

本次明确调整：原“acceptance before merge”改为本次变更级检查与审查，完整产品验收仍由 feature_list.json 管理，SPEC 的实施门槛不变。无对应功能 ID 的工程维护提交使用 `{type}: {description}`，避免虚构 ID。

独立只读审查要求补齐起点上下文与 WIP 保护、目标工作区保护、squash/cherry-pick 整合核对和安全清理；均压缩至现有四条。授权可来自当前请求或既定流程，不引入逐次重复确认。

验证：静态检查及独立实文复核通过，无必须修正项。SPEC/PRD/feature_list.json 与本轮起点字节一致，11 个 passes 均为 false；Claude 单行引用、链接与旧锚点有效，worktree 保持四条。本次不执行实际 worktree 创建/合并/删除，不声称运行演练通过。之前的 Codex 英文版加载探测不证明本次中文内容的行为；Claude 先前账户限制仍是未完成的加载证据。

## 收尾与 Claude 加载复验 — 2026-09-08

当前工程基线已保存为本地提交 `12fce31`，74 个文件纳入版本管理。`make check`（5 项测试）、入口链接、限定范围与凭据模式检查通过；9 个归档源文件哈希不变。暂存区检查发现另一份非哈希归档 `first-release-content-proposal-2026-09-06.md` 的末尾空行，已仅移除多余空行后通过。无远程发布。

Claude 新会话加载复验退出码为 0，正确复述 OpsPilot 名称、独立审查触发范围/全新上下文要求、旧项目 skills 非默认入口、worktree 安全清理条件，以及任务完成不等于功能验收。测试问题没有包含这些答案，也未手动注入 AGENTS 内容。

探测保留 CLAUDE.md 自动发现，关闭工具、hooks 和 MCP，使用 plan 模式、无会话持久化，预算上限 1 美元。此前账户 session limit 已不再阻断本次探测。原始本地输出位于 `/tmp/opspilot-claude-loading-retry.txt`；本段记录关键结果供后续会话读取。

这证明当前共享内容在受限新会话中可读取和理解，不证明所有个人/全局配置完全等价，或隔离审查、合并、验收已被完整行为验证。旧 skills 的原生发现不再是当前环境收尾要求；实际任务生命周期仍需后续验证。
