# C1–C5、tool deadline 与 B4/B7 边界最终独立复审

日期：2026-09-12  
受检提交：`66c4306`（worktree `production-ops-agent-m0-01`，HEAD 与分支已核对）  
复审性质：全新上下文、只读；未运行模型、真实 trace、`m0-otel`、PostgreSQL 或外部查询。

## 范围与依据

复核了 `401dc9b`（Run deadline 分类）、`74d949c`（tool-total deadline 分类）及其后至 `66c4306` 的 C1–C5 修正、测试、B4 控制/trace harness、B7 LangGraph 比较边界，并对照当前 `SPEC.md`、C3 合同、M0 计划、`m0-exit-matrix.md` 和 B4/B5/B7 记录。工作区已有用户文档改动，未触碰或清理。

## 结果

### tool deadline / C1–C5

- `holmes_baseline.py` 的 `tool_remaining()` 在锁内重新读取时钟，并按 scope、Run、单工具和累计工具上限取最小值；Run 剩余不超过 4 秒返回 `deadline reached`，scope 授权期限优先返回 `query authorization deadline reached`，累计工具期限返回 `tool total deadline`。两个新增稳定边界码均在 `KNOWN_BOUNDARY_CODES`，对应回归断言存在。
- C1 的 raw/view 绑定仍要求 evidence id、稳定 raw hash 和受限投影视图一致；漂移继续 fail-closed。C2 的锁后重检、C3 边界码、C4 拒绝 bool deadline、C5 非空字符串 tool-call id 与后续配对校验均未发现放宽权限或把不可信输入当作可信事实的路径。
- C1 hash 分支的直接合法原始文件正例、C2 真实锁竞争/跨截止时间并发仍不是本次离线测试覆盖；应继续标为覆盖缺口，不能升级为运行时证明。

### B4 harness / trace / B7

- B4 harness 的真实实验结果被文档明确限定为有限 PG/协议机制证据：B4-1 重建、B4-2 取消后迟到结果拒绝、B4-3 不兼容状态 handoff；结果、费用未知保留和局限均有原始工件索引。该记录没有把 fixture/探针升级为产品调查质量或 M0 通过。
- B4 trace 脚本只发送受限 DTO，并以同 Run 回读；文档保留 `private_fields_persisted=false`、无 reasoning/凭据的边界。静态检查未发现将 provider 私有字段或凭据写入模型/trace 的新增路径。实际 trace 服务行为本轮未验证。
- B7 LangGraph 候选在依赖不可用或真实图未实现时 fail-closed（`LANGGRAPH_EXTRA_UNAVAILABLE` / `LANGGRAPH_COMPARISON_NOT_IMPLEMENTED`），不伪造比较成绩、不新增主依赖；ADR 与退出矩阵正确保留“待验证/不打开 gate”。

## 可复现检查

在目标 worktree 执行：

```text
.venv/bin/ruff check .
All checks passed!

.venv/bin/python -m pytest tests/test_m0_holmes_round02.py \
  tests/test_m0_step_store_protocol.py tests/test_m0_step_store_versions.py \
  tests/test_m0_trace.py tests/test_m0_trace_archive.py -q
93 passed in 2.49s
```

这些命令未启用 integration opt-in，未连接 PG/OTel/trace，也未产生模型 HTTP。

## 结论

本次未发现 P1/P2 实现缺陷。401dc9b/74d949c 的 deadline 分类修复已覆盖此前遗漏的 `tool total deadline`，当前静态控制流、定向测试与 SPEC 的只读/预算/失败可观察性边界相容。C1 直接 hash 正例、C2 锁竞争运行时证据，以及 B4 trace/真实环境和 B7 候选实际比较仍属于未验证项；不影响本代码批次离线复审通过，也不改变 M0 gate、feature passes 或任何费用/权限授权。
