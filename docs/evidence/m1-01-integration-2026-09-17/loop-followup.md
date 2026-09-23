# 报告：处置 4 条既有机器人发现 + 模型请求 wall clamp

- 任务书：`.../scratchpad/briefs/loop-followup.md`
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-m1-investigation-loop`，分支 `feature/m1-01-investigation-loop`，PR #29。
- 最终 HEAD：`26d0c70`（已推送）。

## 结论

授权范围内的 4 条既有机器人发现，3 条完全复现并修复，1 条部分修复（另一半缺数据基础，已写证据、未修、已在 PR thread 补说明）；模型请求 wall clamp 已实现，独立审查发现首版有真实残余缺口并已采纳修复。PR #29 保持「就绪待用户审核合并」状态，未合并。

## A. 4 条既有机器人发现

逐条先写红测试复现、再最小修复、方向只收紧不放宽、一条一个独立提交，均已执行。

| # | 发现（comment id） | 复现结论 | 提交 | 处置 |
|---|---|---|---|---|
| 1 | `delivered_from_context` 缺 run_id/v4 类型绑定（4039957732） | **成立**（伪造/跨 Run context 被当作已交付证据接受） | `82218ae` | 已修：`type`/`run_id` 任一不匹配即整体丢弃 context |
| 2 | `eligible_time_policies` 对空 `target_refs` 判定过宽（4039960100） | **成立**（`named and named.isdisjoint(...)` 对空集恒假） | `23c7158` | 已修：非 `all_authorized_targets` 时要求非空且相交的 `target_refs` |
| 3 | `current` 策略新鲜度未拒绝负值/未验证完整区间（4039962359） | **部分成立** | `faf80bc` | 负值/未来时间戳子问题已修（`0<=freshness<=max_age`）；「完整 source 区间」子问题**未修，有证据**：`opspilot/tools/executor.py` 的 `TransportResponse`/view 只有单点 `data_as_of`，无 `source_start_at`/`source_end_at`，补字段属 PR #20（`opspilot/tools/`）的 schema 变更，需要它自己的设计评审，非本任务「最小修复」范围 |
| 4 | `_settle`/`commit_step` 丢弃围栏期回复（4039963491） | **成立**（模型回复 content/usage/response_id 完全不落盘） | `871e628` | 已修：`DurableStore.commit_step()`/`MemoryStepStore.commit_step()` 围栏时先记 `late_result` 历史（与 `publish()` 已有语义一致）再报错。PG 实现关键坑：`raise` 必须在 `with self.transaction()` 块外，否则插入随异常回滚——已用真实 PostgreSQL 验证（旧写法下 late_result 行数 0，改法后变 1） |

4 条 PR review thread 均已补充说明当前处置（含第 3 条的部分修复说明），保持 resolve 状态。

## B. 模型请求 wall clamp（`ff8e17b` + `8b65c13`）

- `DeepSeekClient` 新增可注入 `clock`/`opener`（均可选，默认真实实现，唯一生产调用点 `scripts/m1_live_flash_loop.py` 已顺手接上共享 clock）；`_read_capped` 按累计 wall-clock（而非每次 `read()` 各自的 socket 超时）判定是否超出 `call.timeout_seconds`（已是 `min(360s 冻结上限, 剩余 deadline, 剩余 wall)`）。
- **独立审查发现首版残余缺口并已采纳修复**：单次已在飞行中的 `response.read(65536)` 调用本身可能远超预算——`http.client.HTTPResponse.read()` 实际调用 `self.fp.read(amt)`，`io.BufferedReader.read(n)` 会在内部反复读底层 socket 直到凑满 `n` 字节，中途没有机会触发外层判断。审查用合成 `BufferedReader`（1 字节/次内部读，0.05s 延迟）实测：单次 `.read(65536)` 跨 201 次内部读耗时 11.3s，零次外层判断机会；据此指出代码注释「deadline 真正约束了总时长」的说法过度声称。已采纳修复（`8b65c13`）：新增 `_tighten_socket_deadline`，每次 `read()` 前把 `response.fp.raw._sock`（已用 `socket.socketpair()` 实测核对这条属性链与真实 `http.client.HTTPResponse.__init__`「`self.fp = sock.makefile("rb")`」一致）的 socket 超时收紧到剩余预算；够不到该属性链时静默退化到仍然存在的外层判断，不崩溃。同时更正了过度声称的代码注释。

## 测试（均先红后绿，红态用受控方式实际复现）

- 1/2/3：`eligible_time_policies`/`delivered_from_context` 直接单测（此前全仓无直接测试，只被 loop 间接覆盖）。
- 4：内存态 + 真实 PostgreSQL 两条用例。
- 5：新文件 `tests/test_m1_investigation_client.py`（假传输推进假时钟，红态验证：临时删掉判断分支重跑，0.69s 内失败于 `RESPONSE_TOO_LARGE` 而非死循环，随后从备份恢复）+ loop 级「settle unknown」端到端测试 + 独立审查后新增的 socket-timeout 收紧序列测试。

## 验证证据

- 每条修复后单独跑测试确认绿、再跑 `make check` 确认无级联破坏，逐条独立提交；独立审查处置后复跑一遍仍全绿。
- 最终 `make check`：`uv lock --check` 通过；`ruff check` All checks passed；`ruff format --check` 无需改动；`mypy` Success: no issues found in 27 source files；`pytest` **1314 passed, 84 skipped, 2 xfailed**。
- PG 定向：本 worktree 专属 55431 端口全程空闲，`.venv/bin/python -m scripts.m0.postgres_lab start` → `M1_DURABLE_POSTGRES=1 pytest tests/integration -q` → **30 passed, 54 skipped**（含新增 `late_result` PG 测试）；完成后 `postgres_lab stop`；未用 `M0_ENV_FILE`（不涉及真实 DeepSeek 调用）。
- CI：对最终代码提交 `a378114` `workflow_dispatch` 触发，`checks`/`m0-postgres` 均通过（run 35262241065）；其后仅一条纯 docs 提交（`26d0c70`），未重新触发。

## 独立审查（全新上下文 general-purpose 子代理）

未继承本会话讨论，给定五个提交、待审 diff、原始证据（`loop-followup.md`/`lease-wire-33.md`），未以实现者结论引导。核实方式含逐条读码、自跑 `make check`、在真实 PostgreSQL 上把第 4 条的 `persistence.py` hunk 临时还原重跑确认真红后复原、grep 全仓核对第 1/3 条的事实陈述、用合成 `BufferedReader` 实测第 5 条的内层阻塞问题。

结论：1–4 判定「正确」，无发现；5 判定「方向正确但完整性声称过头」——已采纳并修复（见上）。

## PR 与机器人审查处置

- 提交（6 个逻辑变更）：`82218ae`、`23c7158`、`faf80bc`、`871e628`、`ff8e17b`、`8b65c13`（独立审查修复）+ 2 条 `docs:` 收尾，均已推送。
- GraphQL 核查 reviewThreads 共 23 条，全部 resolve，0 未处理；`mergeStateStatus=CLEAN`，`mergeable=MERGEABLE`。
- PR #29 描述已更新（追加「2026-09-17 续」节）。

## 未完成 / 需用户裁定

1. **发现 #3 的另一半（完整 source 区间校验）未修，且不能在本任务里修**：需要 PR #20（`opspilot/tools/`）先给 `TransportResponse`/view 补 `source_start_at`/`source_end_at` 字段并完成其自己的设计评审，之后才能回到 `reports.py` 补上这部分判定。这是一个真实的跨 PR 依赖，不是本任务能自行决定的事——需要用户或后续任务安排 PR #20 一侧的工作。
2. 未改 11 个 feature `passes`、验收步骤、SPEC 门槛陈述；未扩产品权限；未合并 PR（等待用户审核）。

## 任务记录

`docs/tasks/2026-09-16-m1-01-investigation-loop.md`「追加（2026-09-17 续）」节，含逐条处置、测试、独立审查处置全部细节。

---

/private/tmp/claude-501/-Users-shenghuikevin-dev-AI-production-ops-agent/a7d3abb1-8221-4812-bafb-316b8b11e6aa/scratchpad/reports/loop-followup.md
