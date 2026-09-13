# M0-03 最终提交全新上下文独立复验

日期：2026-09-11  
受检 worktree：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`  
受检 HEAD：`9874903508b584589fdb0a0f112e0f83a7773f64`（`docs: record final PR16 review boundary`）

本审查从全新上下文只读复验当前提交，重点覆盖 `0774960` malformed `tool_calls` fail-closed 修复、`46a1fef`/`0babc02` 的辅助 evidence/source guards，以及 A2/A3、B1–B3 的当前文档状态。未修改生产代码、`feature_list.json`、`SPEC.md` gate 或既有历史 evidence；没有运行模型、trace、OTel/Holmes 服务，也没有启动 PostgreSQL。

## 可复现检查

- `git status --short --branch`：`chore/m0-02-convergence`，工作区干净；`git rev-parse HEAD` 与本记录 HEAD 一致。
- 定向回归：
  `.venv/bin/python -m pytest tests/test_m0_step_store_protocol.py tests/test_m0_question_paths.py tests/test_m0_question_scope_binding.py tests/test_m0_holmes_round02.py tests/test_m0_envoy_log_projection.py tests/test_m0_outcomes.py tests/test_m0_outcomes_v4.py -q`
  —— **268 passed in 2.49s**。
- 完整本地检查：`make check` —— doctor/锁文件检查通过；`ruff check .` **All checks passed**；`ruff format --check .` **321 files already formatted**；pytest **723 passed, 44 skipped in 21.10s**。44 个 skip 均为显式 PostgreSQL/合成预算 opt-in，未启动服务。
- 另对本轮受影响 Python 文件执行 `ruff check` 与 `ruff format --check`：**All checks passed；7 files already formatted**。
- `jq` 复核 `feature_list.json`：11 项 `passes` 全为 `false`；`git diff --check` 通过。

## 复验结果

### 1. `0774960` malformed `tool_calls`：通过，未发现新问题

`scripts/m0/step_store.py:26-33` 的 `_validated_tool_calls()` 先要求 provider message 为 mapping，再要求 `tool_calls` 为 list 且每个元素为 dict；否则分别抛出 `ASSISTANT_INVALID`/`TOOL_CALLS_INVALID`。`StepStore.commit_response()` 在进入 ledger transaction 前调用该 helper（`step_store.py:535-550`），因此 malformed container 不会进入 placeholder 构造、provider continuation 或持久化路径。合法容器保持原 list identity；后续 `continuation()` 仍校验每个 call 的 id/type/function/arguments、唯一 ID 和 tool pairing。

`tests/test_m0_step_store_protocol.py` 覆盖 `None`、mapping 以外的 `tool_calls` 和非-dict元素三类拒绝，以及合法容器保留；该测试包含在上述 268 项定向回归和 723 项完整回归中。静态调用顺序与确定性回归共同支持“在副作用前 fail closed”的结论；没有把此离线证据扩大为真实 provider 行为证明。

### 2. 辅助 evidence/source guards：通过，历史发现已处置

- `scripts/m0_environment/initial_evidence.py:38-63` 的 `validate_source_path()` 在读取前拒绝 `.env*`、`.netrc`、`.npmrc`、`.pypirc`、裸 `credentials`、`id_rsa`/`id_ed25519`、`.aws`/`.docker`/`.ssh`/`private-protocol`、最终符号链接和非普通文件；`_bytes()` 只有在该 guard 通过后才 `read_bytes()`。
- `holmes_baseline.main()` 在读取 question 前拒绝 legacy report-only 缺 `scope-file`（`holmes_baseline.py:440-452`），并在读取 scope 中 deployment registry、time-policy 前再次调用 `validate_source_path()`（`holmes_baseline.py:474-495`）。测试用 read-spy 证明拒绝发生在 question 读取和 child dispatch 之前。
- `tests/test_m0_question_paths.py:157-192` 覆盖 legacy report-only 无 scope 的读前拒绝；参数化路径（`tests/test_m0_question_paths.py:225-246`）现在包含普通目录 basename `id_ed25519`、`.pypirc`、裸 `credentials` 以及 private-parent 变体，已消除此前被 `.ssh` 父目录规则遮蔽的覆盖缺口。
- `outcomes_v4` 的 `_user_messages_match()` 对 envelope 的 `evidence_views` 做完整可信 delivery 等值比较；`input_provenance_errors()` 对 imported view append 只接受 trusted evidence ID，并要求完整 view 等值或其 raw artifact 的受信 `raw_hash` 命中。新增 tamper/完整-view 回归均通过。

因此此前 `P2-A3-TEST`（basename 用例被父目录规则遮蔽）和 auxiliary evidence view/source findings 在当前 HEAD 有代码、回归和读前顺序证据支持，未发现新的 P1/P2。历史审查记录和失败样例保持原样，没有重写为历史通过。

### 3. A2/A3 文档与实现状态：一致

- A2 的 `normal02` v4 log projection pin 仍由固定 fixture 测试约束：backend 20 行、model-visible 14 行、显示/省略合计 20，序列化 view 不超过 14,000 bytes；Envoy 标签接入后再按同一上限裁剪。该行为在定向回归中通过。
- A3 parser docstring/实现保持 pinned token、时间、方法、状态和数值校验，同时容忍 shell quoting 与重复空白；scope binding 只做 best-effort 顶层字段检查，不递归把嵌套业务 JSON 当授权。对应 round-02/round-03 回归通过。
- `docs/testing/first-investigation-v4-2026-09-10.md` 的 B2 校准段已使用可审计计数：M002 8 条 per-run、其中 6 条有 token、2 条 usage missing；M003 工具数 11–20；M004 工具数 9–12。缺失 usage 保持 unknown，候选上限仍明确“待用户批准”，没有冻结旧排演值或恢复 aggregate cap。

### 4. B1/B2/B3 文档状态：诚实保留 partial/缺口

- `m0-exit-matrix.md:7-18` 将工作包 1–7 标为“部分”、工作包 8 标为“证据不足”，逐项列出真实/替身层级和缺口；28h 的 M1-01 拆分明确是待 gate 决定的实施准备，不是已开始实现。矩阵明确禁止将 partial/证据不足汇总为 M0 通过。
- `m0-b3-deterministic-contracts.md:5-27` 将 no-data/stale/缺 profile/观察缺口的离线 unknown 合同与专属 PG 子集（StepStore 27、Budget 9、restart 1）分开记载；DB 短故障全链路、发布竞争、HealthProfile 乱序、暂停/resume、单独观察并发仍标证据不足，且服务停止、数据保留边界清楚。
- `ROADMAP.md:102-106` 保持 B1、B2、B3 `[ ]` 索引状态、SPEC gate `not cleared`、PR16 不自动合并，并将 tip 更新为 `0774960`、本地 `723 passed/44 skipped`。`SPEC.md` 的 implementation gate 未被放宽；11 项 feature passes 仍全 false。

## 独立结论

在本次只读范围内，`0774960` malformed container fail-closed、辅助 evidence/source guards、A2/A3 实现与 B1–B3 文档状态均通过当前代码、测试和文档交叉核对；**未发现新的 P1/P2，也未发现 gate/passes/权限边界被放宽**。此前 P2-A3-TEST 与 P2-B2-STATS 的历史发现已由相应修复和前序独立复验记录处置，原始发现正文保留。

本结论仅是当前提交的离线/静态/确定性独立复验，不是模型、trace、服务、PostgreSQL 全链路或生产证明；M0 partial/unknown、B4–B8 缺口、SPEC gate not cleared、PR 用户审核/合并边界继续有效。
