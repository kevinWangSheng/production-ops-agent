# 模型 profile 切换到 V4.1 Flash（`deepseek-flash`）

- 状态：进行中
- 更新日期：2026-09-15
- 依据：用户 2026-09-15 决定（对应[指令与工具接口合同提案](../design/instruction-and-tool-interface-contract-2026-09-15.md) §5 的 U1）；
  官方 [Change Log](https://api-docs.deepseek.com/updates/) 2026-09-10 条目；
  [SPEC.md](../../SPEC.md)「Model priority and design ownership」；[C3 第 5 节](../design/technical-proposal-2026-09-07.md)。
- 工作区：`chore/model-profile-v41` @ `/Users/shenghuikevin/dev/AI/production-ops-agent-model-v41`

## 目标与范围

把出站模型请求名从 `deepseek-v4-flash` 改为官方当前名 `deepseek-flash`。

**为什么不是 `deepseek-v4.1-flash`**：官方 Change Log 原文是
*Change the model name to `deepseek-flash` to call the latest V4.1 Flash model*，
且官方 models 端点只列 `deepseek-flash` 与 `deepseek-v4-pro`。
`deepseek-v4.1-flash` 只出现在本仓库 `scripts/m0/live.py` 的响应名允许表与第三方聚合站，
不是官方可调用 id。

**此改动只换请求名，不换后端。** 官方 2026-09-10 声明 V4 Flash 已退役、
`deepseek-v4-flash` 仅临时路由到 V4.1 Flash；仓库证据一致——
2026-09-10T02:19Z 之后的全部已记录响应都回报 `deepseek-flash`。
因此这是把「带失效时钟的临时别名」换成「规范名」，不是模型代际迁移。

范围界限：不改验收步骤、不改 `feature_list.json` 的 `passes`、不重新校准冻结值、
不改带 allocation id 的历史实验脚本。

## 前提与完成条件

- 前提：只读核对官方文档与仓库证据，本任务**不发起任何真实模型调用**，无费用。
- 完成条件：前瞻性配置全部切换；`make check` 通过；权威来源（SPEC、C3）记录决定与依据；
  冻结包的原文保留并加注；ROADMAP 反映实际状态；独立审查完成且发现已处置。

## 改与不改的划分

**已改（前瞻性——决定下次真的调用什么）**

| 文件 | 内容 |
|---|---|
| `scripts/m0/config.py` | `PROFILE["OPSPILOT_MODEL"]` |
| `.env.example` | `OPSPILOT_MODEL` |
| `scripts/m0/live.py` | `MODEL_PROFILE` 的 `request_model` 与 `accepted_response_model` |
| `scripts/m0/adapters.py` | 出站 `chat.completions.create(model=...)` |
| `tests/test_m0_live.py` | 响应名合同测试的正常分支替身值 |

**已改（权威来源记录决定）**：`SPEC.md`、`docs/design/technical-proposal-2026-09-07.md`、`ROADMAP.md`。

**加注但保留原文**：`docs/testing/first-investigation-v4-2026-09-10.md`——
冻结包「接口仍为显式 `deepseek-v4-flash`」原文不改写，
另加 2026-09-15 更新块说明仅模型请求名一项被取代，其余冻结项与验收步骤不变。

**不改（历史实验合同）**：`scripts/m0_environment/round02.py`、`round03.py`（带 allocation id
`m0-02-20260910-convergence` / `m0-03c-20260911-normal-facts`）、
`scripts/m0_lab/round07/candidate_runner.py`、`upstream_runner.py`（证据含 `runner_sha256`）、
其余 probe 脚本与历史 fixture 断言。改写这些会让已记录证据与代码不一致，
等同于「用新版本解释旧模型当时看到的内容」。

## 执行进展与证据

| 检查 | 命令 | 结果 |
|---|---|---|
| 全量测试 | `.venv/bin/python -m pytest tests/` | **1038 passed, 75 skipped, 2 xfailed** |
| 响应名合同 | 同上，`test_m0_live.py::test_complete_boundary` | 通过；正常分支替身值改为 `deepseek-flash` |
| 残留字面量 | `grep -rn deepseek-v4-flash`（排除 docs/.venv） | 仅存于历史实验脚本与历史 fixture，符合上表划分 |

**响应校验收紧的副作用（有意保留）**：`live.py` 的校验是精确匹配 fail-closed
（`response.get("model") != profile["accepted_response_model"]`）。
切换后请求 `deepseek-flash` 时，回报已退役的 `deepseek-v4-flash` 将被**拒绝**。
这比原先的双名允许集更严，不是削弱；原双名允许集是 2026-09-10 针对
「请求旧名」情形的决定，请求规范名后不再适用。

**未执行**：真实模型调用。本任务不验证新请求名在真实端点上的行为，
该验证需要单独的实验授权与预算合同。**因此「切换已完成」不等于「新 profile 已验证」。**

## 下一步与交接

1. **真实调用验证仍未做**：需一次有界真实请求确认 `deepseek-flash` 在官方端点可用且回报同名。
   在此之前，SPEC 中该 profile 的「availability 与协议兼容性需 M0 验证」条款仍然成立。
2. 独立审查：本任务涉及权威来源（SPEC/C3）变更与 fail-closed 校验收紧，需独立审查后再提 PR。
3. [合同提案 PR #24](https://github.com/kevinWangSheng/production-ops-agent/pull/24) 的 §5 U1
   已由本决定裁定；该 PR 合并前后需同步把 U1 标记为已决，避免留下未决项的陈旧表述。
4. 本任务未启动任何进程或服务；worktree 在 PR 合并且确认整合后清理。
