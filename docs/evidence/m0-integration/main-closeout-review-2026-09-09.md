# M0 主线汇合独立复核（2026-09-09）

审查者为全新上下文 `review_main_merge`，未参与实现或冲突处理。范围是已获授权的 #4–#7、#9 及既有 #8 的依赖整合与当前状态更新。结论：本次受检范围未发现阻断项；离线检查通过，最终 PR/main CI 与扫描须由协调者完成，不能据此宣布 M0 退出或产品验收。

## 受检对象与依据

- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-main-closeout`，分支 `chore/m0-main-closeout`。
- HEAD：`32f37b55c8292bd823041339e73a7aaa21eac378`，加审查开始时 ROADMAP 与 A/B/C/batch/integration 六份任务状态文档的未提交修改。
- main：`b096e7b72d4b2995d027331c252e59dbc715c9a8`；components：`496d1b52991f377350dfdb32cbb452b8307e665f`。
- 第一次普通合并 `9e30d72f66b98b23d6960093ffb21004271cb126` 的父为 components 和 `46d9d9b`；第二次合并 HEAD 的父为 `9e30d72` 和最新 main。
- 已读 AGENTS、完整 SPEC、ROADMAP、批次与集成任务、#4/#6/#7 修复及基线合入独立报告、#5 状态复核、#8 集成独立报告与 #9 固定版本证据复核。核查 SPEC 排除、证据、人工控制、费用、交付与实施门槛保持不变。

## 实际验证

1. `git status --short`、双向 `git diff --stat origin/main` / `origin/chore/m0-components` 与当前文档 diff：只有预期已审增量及状态汇合；没有新增产品实现、依赖或验收修改。
2. Python 对所有 tracked `scripts/`、`tests/`、`.github/workflows/` 及 Makefile、pyproject.toml、uv.lock 逐字与两侧 Git blob 比较：每份至少匹配一侧；32 份匹配 main，23 份匹配 components，计数可重叠，无不匹配项。#4 配置/MCP、#6 模型取消及 #7 v2 公开合同的关键文件 SHA256 与原独立报告最终值一致，见下表。
3. main 的 58 份 `docs/evidence/` 文件全部逐字保持。components 的 48 份中 47 份逐字保持；唯一差异 `m0-a/README.md` 为 main 原有 PR #6 追加说明，原内容前缀保留、当前整份与 main 一致。原失败输出、v1 schema、旧 verification 与 #9 固定版本工件未被改写。
4. `git show --remerge-diff --stat 9e30d72` 仅显示 ROADMAP 和批次索引的冲突处置；对 HEAD 执行同一命令无额外处置。当前文本同时保留 components 历史清单与 main 修复记录，页首/末段清楚区分当前状态和历史待办。
5. `make check` 退出 0：doctor、离线 lock、Ruff lint/format 全通过，127 files already formatted；pytest 199 collected，**186 passed / 13 skipped，1.48s**。skip 为 3 项 adapter+PostgreSQL 集成和 10 项 PostgreSQL/原生 restart 显式 opt-in；未将其计作数据库通过。
6. `git diff --check -- ROADMAP.md docs/tasks` 退出 0。对六份当前状态文档解析本地 Markdown 文件链接，除当时尚未写入的本报告外全部存在；报告写入后复核全部存在。SPEC.md、PRD.md、feature_list.json 与两侧均逐字一致。
7. `gh pr list --state merged --limit 12 --json number,mergeCommit,baseRefName,url` 实时确认 #4/#5/#6/#7 已合入 main，分别为 `f9f526b`、`b46edb7`、`46d9d9b`、`b096e7b`；#8/#9 已合入 components，为 `1282ab4`、`496d1b5`，与当前状态文档一致。

## 关键源码 SHA256

- `scripts/m0/config.py`：`c9b14626e1982af21f9b1df927c74eec237fd5278f8a20c206ea0d3468369a8e`。
- `scripts/setup_docs_mcp.py`：`8c51a8cf503e643be4209e608acee209f85e052508b71749438a435fb0db35dc`。
- `scripts/m0/adapters.py`：`38748c92786b0bf45f0595fd7ea72e975512cddf0fa2936cba8f02bb73abc88b`。
- `tests/test_m0_adapters.py`：`e06949f7820df61e4dcddae791f7a276d53dea0ff0d1996f9b36e861b7dc5522`。
- `scripts/m0/outcomes.py`：`79d0fb80c82d5e72ff95a93567112712e9413a535edeb9d0358e2559a899f992`。
- `tests/test_m0_outcomes.py`：`705fb8653162d9b13903fce5e4ede94cc9d5c9b7f999edbe15291b82eaa5570d`。
- `docs/evidence/m0-c/IncidentOutcome.v2.schema.json`：`01880901784ef5c4ac8022cd6e4511cd2d059b1db5108253eb498f6601ea2da9`。
- `docs/evidence/m0-c/IncidentScenario.v2.schema.json`：`e571571d7f59ef13388a930614c48786b4da456d3363e2a8420c5ef6cdf2af65`。

## 当前状态文档 SHA256

- `ROADMAP.md`：`e83476194c65c2515edfe0e697022572106930aac1f0f5249b1f6997e0d76dda`。
- `docs/tasks/2026-09-08-m0-a-adapters.md`：`f7351b2a6e6590ad172fa44769658ca6c799ebd392ad0b5021c092d6ca65e728`。
- `docs/tasks/2026-09-08-m0-b-budget.md`：`99442ba4e67b1b955e848d0c42b0b46cb0cd4f1bad865a58d985f718e125b934`。
- `docs/tasks/2026-09-08-m0-batch.md`：`dddf3c29f828d2626faf3dc962d34c0b2f6a08b5a488775538942f10f6145d6f`。
- `docs/tasks/2026-09-08-m0-c-outcomes.md`：`02c3ebc18b626a7e6290d2717f366f1d991349251395da4b9807d1e5b47d10b8`。
- `docs/tasks/2026-09-08-m0-integration.md`：`2a1ba65f58b65544d7746916ad18ea9386334b5059b93e78459460589a28f1f9`。

## 限制与交接

合并授权来自协调者携带的用户明确授权及任务记录；本审查核对范围，没有授予新的合并、部署或费用权限。没有读取真实 .env、模型/trace，未启动 PostgreSQL 或其他服务，未 commit/push，也未修改历史证据或实现。仅新增本报告。

本次没有重复运行 Gitleaks 或真实数据库；旧独立结果属于其固定版本，最终暂存快照扫描、最新 PR CI、main CI 与合并后本地同步/安全清理由协调者执行并留存。工作区端口及其他任务私有数据的保留状态由协调者核验，本审查不将任务描述当作独立进程观察。没有重新认证完整安全边界、真实服务兼容、产品恢复、M0 退出、评测或 soak；当前 SPEC 实施门槛仍关闭。
