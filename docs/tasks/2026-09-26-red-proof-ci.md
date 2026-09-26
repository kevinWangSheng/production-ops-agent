# 测试红证明 CI

- 状态：试行中（#49、#50 已合并）
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
- 独立审查（全新上下文 Agent）：历史回放数字逐项复核一致。P1 `--report-only` 下超时/内部异常仍非零退出且不落报告——采纳：超时与 junit 解析失败记为「未运行」，其余异常统一落「内部错误」报告，试行期恒 0。P2 只改 `tests/*_support.py` 时静默判不适用——采纳：改报「需人工确认」并列出未重放的 tests/ 改动。P3 采纳 node_id 转义、dispatch 并发组按 head 分组、文档写明退出码；「`no-red-proof` 由谁打标签」留到改为阻塞前评估。三类修复各有回归用例（10 passed）。
- PostgreSQL 路径（合并后补验）：本机 55431 属于 web-worker worktree，未在本地验证；合并后 [CI 回放 #47](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/36235985266)（`base=1ece309 head=65a5380`）：14 个新增/改动测试全部在 base 上失败，本地记为跳过的 10 个集成用例在 CI 中实际执行。
- 回放暴露的分类局限：14 条里 10 条是 `TypeError: ... unexpected keyword argument 'renew_run_id'`——新接口在 base 上不存在，证明力与收集失败同级，却被计为「断言失败」；真正的行为级失败是 `DID NOT RAISE`、`409 == 200` 与 ID 断言。试行期不改规则，结束评估时决定是否把「调用不存在的接口」并入弱红证明。

## 下一步与交接

1. 试行至 2026-10-10 前后：汇总各 PR 的红证明结论，统计无红证明、需人工确认的实际原因，以及上述接口缺失类「断言失败」的占比。
2. 据此决定：是否去掉 `--report-only` 改为阻塞；是否细分接口缺失类红证明；改为阻塞前评估 `no-red-proof` 标签由谁添加。
3. #50 的验收测试作者分离从下一个功能 PR 起生效，首个适用 PR 在其任务记录中注明测试作者与实现者分离的方式。
- 清理：`chore/red-proof-ci`、`chore/review-finding-scope` 两个 worktree 与本地分支已按内容核对并入 main 后移除；远程分支保留。
