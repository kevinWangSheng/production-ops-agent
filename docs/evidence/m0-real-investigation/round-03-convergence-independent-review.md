# M0-03 收束提交独立复验（A2/A3、B1–B3）

日期：2026-09-11  
受检 worktree：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`  
受检 HEAD：`44932c9dd887e90d15a70fe6ad0f5424a2efb10d`（`chore: converge M0 evidence and PR16 review`）

本审查在全新上下文执行，只读检查当前提交的代码、测试、任务/计划、退出矩阵和已提交 evidence；没有运行模型、trace、OTel/Holmes 服务，也没有启动 PostgreSQL。没有修改生产代码、`feature_list.json`、SPEC gate 或既有历史证据。

## 执行的检查

- `.venv/bin/python -m pytest tests/test_m0_holmes_round02.py tests/test_m0_envoy_log_projection.py tests/test_m0_question_paths.py tests/test_m0_question_scope_binding.py tests/test_m0_outcomes.py -q`：**167 passed**。
- `.venv/bin/ruff check`（本轮 6 个变更 Python 文件）：**All checks passed**。
- `.venv/bin/ruff format --check`（同上）：**6 files already formatted**。
- 已读 `SPEC.md` 的 gate/排除/证据/运行控制/验证交付条款、M0 计划八个工作包、任务记录和 `m0-exit-matrix.md`、`m0-b3-deterministic-contracts.md`；并用 `jq` 重算 `round-02-final-usage.json` 的 `per_run` 统计。

## 结果摘要

### A2：normal02 v4 行数 pin — 通过

`tests/test_m0_holmes_round02.py::test_actual_log_raw_distinguishes_backend_and_visible_records` 使用固定 `normal02_log_raw.json`，调用默认 `log_projection()`（`m0-03-logs-v4`），断言 backend 返回 20 行、`model_visible_hit_count == 14`、显示与省略计数相加为 20，且序列化 view 不超过 14,000 bytes。实现先加入 Envoy 标签再按同一上限裁剪，最后同步可见/省略计数；因此该 pin 与当前 v4 行为一致。

### A3：parser、scope top-level 语义和 denylist — 代码/大部分回归通过，存在 1 项覆盖缺口

- `_envoy_access_fields()` 的 22-token/时间/方法/状态/数值位置校验与 `shlex.split(posix=True)` 的 shell quoting、重复空白语义和 docstring 一致；v4 仅为符合服务、`proxy.access` 的行加标签，v2/v3 replay 保持旧投影。
- `validate_question_scope_binding()` 只检查 JSON 对象顶层的 `window`/`requested_window`、`scope_revision`、`run_id`；嵌套业务 JSON 不递归且不被当作授权。匹配、顶层陈旧字段、嵌套陈旧字段测试均通过，和 docstring/任务记录一致。
- `validate_source_path()` 当前会拒绝 `.pypirc`、裸 `credentials`、`id_ed25519` 等 basename；敏感目录（`.aws`、`.docker`、`.ssh`）仍拒绝，移除原来对 `.aws/.docker` 下特定文件的冗余分支不改变实际拒绝效果。

**发现 P2-A3-TEST（测试覆盖缺口）：** 任务记录称新增了 `.pypirc`、`id_ed25519`、裸 `credentials` 三个读前拒绝用例，但参数化测试使用的是 `.ssh/id_ed25519`。该样例会因父目录 `.ssh` 命中 `private_dirs`，即使从 `private_names` 删除 `id_ed25519` 测试仍会通过，不能证明 basename 规则本身被覆盖。当前实现对裸 `id_ed25519` 的行为静态上正确，但应增加根目录（或普通目录）下的 `id_ed25519` 用例并保持读前 spy；在此修复前，不应把“三用例”称为完整 denylist 回归。

### B1：八包矩阵与 M1 工时 — 通过（状态表达诚实）

`m0-exit-matrix.md` 将工作包 1–7 标为“部分”、工作包 8 标为“证据不足”，逐项列出现有真实/替身证据和缺口；没有把 partial、默认 skip、环境缺测或局部 PG 证据汇总为 M0 通过。M1-01 拆分明确为实施准备，28 h 标注为单工程师有效工时、排除用户审核/CI/真实环境等待，并写明待 B4–B8 与用户 gate 决定后重校准。因此没有发现将 partial 误称通过或将估算误称已实施的问题。ROADMAP 与任务记录也保留 `SPEC gate not cleared`、feature passes 不变和未开始 M1 的边界。

### B2：候选预算/时序记录 — 候选状态诚实，但统计叙述有 P2 事实错误

文档正确把 `128KiB/8192/4/20/180s/20s/780s` 标为旧排演值，把 `512KiB/2MiB/16384/4/20/360s/1800s/30s/240s` 标为候选，并明确工具累计/清理上界、逐请求 sidecar 和供应商账单仍缺，最终值待用户批准；未把候选写成冻结授权。

**发现 P2-B2-STATS（校准分布与保留账本不一致）：** `docs/testing/first-investigation-v4-2026-09-10.md` 的校准段写“`M002` 的 7 条有 token/timing 记录”且 completion 范围为 `0–15,104`，并写 `M003` 为 `3–4 HTTP/9–20 工具`。独立按提交中的 `round-02-final-usage.json` 重算：`per_run` 有 8 条，其中 2 条 `usage_missing_requests=1`；6 条有实际 token 的记录为 prompt `603–106,736`、completion `138–15,104`，缺失项的 0 不能作为实测下界。提交内可回读的 M003 报告/审查计数为 `m003b 4/18`、`m003c normal 3/13`、`m003c normal 3/16`、`m003c fault 4/20`、`m003d 4/16`、`m003e normal 3/11`、`m003e fault 4/14`；未找到工具数为 9 的 M003 Run（9 是 M004 fault 的工具数）。因此“7 条/0 completion/9–20”会误导预算校准，尽管该段仍明确是候选且待批准。应修正为可审计的样本计数，并将 missing usage 保持为 unknown（或显式给出不含缺失值的范围）；M003/M004 的每项计数应按 Run 列表逐项引用。

### B3：no-data/stale/profile 语义和真实 PG 证据 — 状态表达诚实，证据层级边界保留

- `test_health_requires_independent_current_complete_observation` 覆盖 `no_data`、stale、缺 profile、缺 signal、样本不足、错误状态、未来采样和超 deadline；所有路径要求 `independent_health == unknown`，并拒绝提前 healthy。
- `HealthProfile` 的最小字段、重复 signal 拒绝及 v4 paused/cancel/correct/new_run/迟到提交失败样例均保留；这些是离线确定性合同，不冒称真实恢复证明。
- 已提交 PG 输出为 StepStore **27 passed**、Budget **9 passed**、显式 restart **1 passed**，并有 start/stop 输出；测试代码固定使用 `scripts.m0.postgres_lab.DSN`（127.0.0.1:55431、`m0_budget`），且任务记录写明专属实例停止、数据目录保留。该证据支持“本轮真实 PG 子集运行过并已停止”的有限结论，不支持完整 ModelStep/ToolOperation 短故障组合、发布竞争、HealthProfile 乱序、暂停/resume 或单独观察并发通过。
- `m0-b3-deterministic-contracts.md`、退出矩阵和 ROADMAP 明确将上述缺口标为部分/证据不足，未把真实 PG 子集或离线 unknown 语义写成八项机制全部通过；这一边界符合 SPEC。

PG 文本输出本身没有重复记录完整命令、HEAD 和服务身份检查的 stdout；身份与端口依据来自固定测试代码、`postgres_lab.py` 和任务/evidence 记录。后续若将这组结果用于 gate，建议保留带命令、环境变量、版本/端口和 stop 后监听检查的 sidecar，但当前文字没有把它扩大为产品或完整 M0 证明。

## 独立结论

当前 HEAD 的 A2 实现与 pin、A3 parser/scope 语义、B1/B3 状态边界和候选待批准声明均可由代码/文档/测试相互核对；未发现生产权限、SPEC gate 或 feature passes 被放宽。存在两项需处置的证据/回归问题：

1. **P2-B2-STATS**：预算校准段错误地把缺失 usage 当 0，并给出无法由提交内 M003 Run 支持的 `9` 工具下界；这必须在接受校准或冻结预算前修正文档。
2. **P2-A3-TEST**：`id_ed25519` 测试被 `.ssh` 父目录规则遮蔽，三项 denylist 用例尚未真正覆盖 basename 规则；应补普通目录/根目录样例。

在上述问题修复和复验前，本审查不建议将 A3 denylist 回归称为完整通过，也不建议把 B2 分布作为最终冻结依据。B1/B3 的 partial/证据不足、五条 telemetry unknown、M0 gate not cleared 和 M1 未开始状态应继续保持。

## 修复后复验（2026-09-11）

协调者补充了普通目录下裸 `id_ed25519` 的 read-spy 用例，并将 v4 校准段改为 M002 8 条 per-run/6 条有 token（completion 138–15,104、2 条 missing）、M003 11–20 工具、M004 9–12 工具。相关定向测试与完整检查在新提交上重跑；以上两项 P2 已处置，历史发现正文保留不改。
