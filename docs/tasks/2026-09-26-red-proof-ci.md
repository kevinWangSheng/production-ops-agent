# 测试红证明 CI

- 状态：进行中
- 更新日期：2026-09-26
- 依据：用户 2026-09-26 决定先落地「红证明 CI + 审查只报正确性/合同缺口」两项；业界依据见下方「必要上下文」
- 工作区：`chore/red-proof-ci`，`/Users/shenghuikevin/dev/AI/production-ops-agent-red-proof`

## 目标与范围

PR 正文里自述的「红→绿」无法复核。把它变成可重放检查：PR 新增或改动的测试放到 merge-base 代码上，至少一个失败。
本任务只交付检查脚本、测试、试行期只报告的工作流与开发指南入口；不改已有测试，不改变合并门槛。
审查约束（只报影响正确性或合同的缺口）走单独的 AGENTS.md PR。

## 前提与完成条件

- 前提：`tool.uv.package = false`，`opspilot` 不装入 venv，快照内 import 解析到 base 代码；脚本在运行前断言这一点。
- 完成条件：脚本单测通过且承重用例有区分力；历史 PR 回放结论合理；PR CI 通过、独立审查完成；合并后 CI 回放 #47 的 PostgreSQL 用例得到非「跳过」结果。

## 必要上下文

- fail-to-pass 判据：[SWT-bench](https://arxiv.org/abs/2406.12952)；agent 自报通过证据 46% 无区分力：[arXiv 2607.28871](https://arxiv.org/abs/2607.28871)
- 审查过度导致冗余测试：[Claude Code best practices](https://code.claude.com/docs/en/best-practices)「Add an adversarial review step」

## 执行进展与证据

- 设计取舍：按文件逐个运行 pytest——实测任一文件收集失败时 pytest 以 exit 4 中止整次调用，其余文件不跑。试行期退出码恒 0，结论写入 step summary 与 notice/warning 注解：非必需检查失败会使 `mergeStateStatus` 变为 UNSTABLE，与就绪定义冲突。
- 单测：`tests/test_red_proof.py` 7 passed。把快照改成取 HEAD 实现后，3 条依赖 base 实现的用例变红，复原后全绿。
- 本地回放（未开 PostgreSQL，集成用例记为跳过）：#44 断言失败 3 / 收集失败 4 / 跳过 12；#45 断言失败 1 / 收集失败 9 / base 上通过 2；#46 断言失败 1；#47 断言失败 4 / 跳过 10；#48（只补测试）无红证明、跳过 3，属 `no-red-proof` 适用情形。
- 未执行：本机 55431 的实例属于 `production-ops-agent-web-worker` worktree，端口在 `scripts/m0/postgres_lab.py` 写死，未启动第二个实例；PostgreSQL 路径留待合并后回放。

## 下一步与交接

1. PR：CI、独立审查、`@codex review` 分诊。
2. 合并后：`gh workflow run red-proof.yml -f base=65a5380^ -f head=65a5380`，核对 PG 用例结果。
3. 试行 1–2 周后统计误报与「无红证明」的实际原因，决定是否去掉 `--report-only`。
