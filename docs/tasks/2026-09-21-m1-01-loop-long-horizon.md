# M1-01 调查 loop 长程执行边界改造（设计阶段）

- 状态：**设计草案已完成独立审查并修订，待用户决定 PR 策略与压缩方式后进入 P1 实施**
- 更新日期：2026-09-21
- 前序：[Flash 调查 loop 任务记录](2026-09-16-m1-01-investigation-loop.md)、PR #29（`9f3506f`，`CLEAN`，待用户审核）
- 依据：SPEC 有界开放 M1-01；PRODUCT-CONSTRAINTS；C3 §5/§7/§13；ADR-0002/0003/0004；
  v4 冻结包 B2 段；[上游对标调研](../research/upstream-agent-loop-benchmark-2026-09-21.md)
- 工件：[设计草案](../design/investigation-loop-long-horizon-2026-09-21.md)
- 工作区：worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-m1-investigation-loop`，
  分支 `feature/m1-01-investigation-loop`；本记录与草案只本地提交、未推送

## 目标与范围

把 PR #29 的单 Run 内存 loop 改造成：从 PostgreSQL 业务行重建模型上下文并继续、可测量的上下文预算与确定性折叠、
模型/工具/活跃时间/上下文预算分离且重启不重置、显式 handoff 与跨 Run 续接上下文构造器。
范围外：UI、intake、observer、postmortem、自动创建新 Run、修改 11 个 `passes`、修改 v4 冻结值、引入 LangGraph 或 Agents SDK。

## 前提与完成条件

- 前提：用户对草案第 9 节两项作决定（PR 策略 A/B；折叠 vs 保留 LLM 摘要）；P2 前置一次真实折叠冒烟走常设授权。
- 完成条件：草案第 10 节 P1–P3 全部用例先红后绿；`make check` 与 PG 定向通过；独立审查完成并处置；
  任务记录、ADR-0004 复核注记、PR 描述更新。

## 执行进展与证据

- 2026-09-21：读取 #29 分支、main 已合并的 #30 恢复层、C3/ADR/PC/v4、上游一手源码
  （HolmesGPT `773fddf`、OpenSRE `4303874`、OpenAI Agents SDK `ad93f54`、LangGraph 官方文档）；写出草案。
- 2026-09-21：全新上下文子代理独立审查，结论「需修改后实施」，七项取舍成立；
  4 处实质缺口（tools 面只存 sha、活跃时间结算方式、同 Run follow_up/correct 后旧代际悬空组、
  非报告终态无持久写路径）与若干事实修正已全部写回草案第 2/3/4/6/7/8/10 节，见草案第 11 节。
- 未运行任何代码、测试或真实模型；本阶段只有静态源码与文档证据。

## 下一步

1. 用户决定草案第 9 节两项。
2. 按决定开始 P1（持久状态与恢复），先写 5 个断点的子进程 kill 用例与 fake ModelClient。
