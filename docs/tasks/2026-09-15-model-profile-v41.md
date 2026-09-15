# 模型 profile 切换到 V4.1 Flash（`deepseek-flash`）

- 状态：进行中
- 更新日期：2026-09-15
- 依据：用户 2026-09-15 直接决定。（同一问题也出现在 PR #24 的合同提案 §5 U1，
  但该提案**尚未合并、本分支不存在该文件**，因此不作为本次变更的依据引用。）
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

**此改动只换请求名，不换后端——但这依据供应商文档，不是逐 Run 身份记录。**
官方定价页原文：旧名「still accepted, but the corresponding models have been retired,
their requests are served by the DeepSeek-V4.1-Flash model」；
2026-09-10 Change Log 另称旧名为 temporarily routed。
因此这是把「带失效时钟的临时别名」换成「规范名」，不是模型代际迁移。

**仓库证据的实际边界（独立审查更正）**：响应名分界只能定位到区间——
`flash-results.json`（`ended` 2026-09-09T17:18:01Z，回报旧名）到
`m002-saved-report-02`（2026-09-10T02:26:34Z，回报新名）之间约 9 小时。
首个校准 Run `m002-saved-report-01` 起始于 2026-09-10T02:19:02Z，**位于该区间之内而非其后**，
且其响应因身份校验失败未保留。M002 的 8 条 per-run 记录中仅 3 条有回报名，
4 个调查 Run 完全没有模型身份字段。另 B2 冻结的 wall-time/token 值实际取材自 M002，
M003/M004 的 sidecar 未提交（`round-07-wall-time-bound.md` 记为证据不足）。
初稿写成「取材自其后的 M002–M004」「02:19Z 之后的全部已记录响应本就回报 deepseek-flash」，
是把推断写成已核查事实，已在 SPEC/C3/ROADMAP/冻结包四处更正。

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

**响应校验：这次实际是修了一个潜伏缺陷，不是"收紧"。**
`live.py` 的校验一直是单值精确匹配 fail-closed
（`response.get("model") != profile["accepted_response_model"]`），
**从来没有双名允许集**——双名允许集在 `round02.py`/`round03.py` 的 `Profile.reported_models`
与 `round-02-provider-identity-decision.md`，是另一套机制。初稿把两者混为一谈，已更正。

后果是：`live.py` 原本只接受 `deepseek-v4-flash`，而供应商自 2026-09-10 起回报
`deepseek-flash`，因此该文件对真实端点已处于必然失败状态。本次切换顺带修复了它。
切换后请求 `deepseek-flash`，回报已退役的 `deepseek-v4-flash` 会被拒绝，这是正确的 fail-closed。

**同一文件的账本识别集也有漏**（独立审查发现）：`token_usage()` 的 `known` 集合
原含 `deepseek-v4.1-flash`（一个**并不存在的官方 id**）却不含 `deepseek-flash`，
而它在准入校验之前逐响应调用。不修的话，切换后每条真实响应都会被记成
`unreported_or_unrecognized`。已补入 `deepseek-flash`/`DeepSeek-Flash` 并移除虚构 id，
保留历史回报名以便回读旧记录。这个漏正是"V4.1 Flash 到底叫什么"没查清官方文档的直接后果。

**未执行**：真实模型调用。本任务不验证新请求名在真实端点上的行为，
该验证需要单独的实验授权与预算合同。**因此「切换已完成」不等于「新 profile 已验证」。**

## 独立审查与处置

审查者以全新上下文启动，未参与改动。

| # | 发现 | 判定 | 处置 |
|---|---|---|---|
| R1 | `deepseek-flash` 是正确目标名；`deepseek-v4.1-flash` 非官方 id | **成立** | 无需修改。审查独立复核 Change Log 与定价页，两页均无 `deepseek-v4.1-flash` |
| R1b | SPEC 只写旧名「已退役」，漏掉定价页「still accepted」 | **采纳** | SPEC/C3 补全：旧名今天仍可用，但带未标注期限的失效风险 |
| R2 | 「只换请求名不换后端」把推断写成已核查事实 | **采纳** | 实测确认：分界是区间而非 02:19Z 时点；M002 起始于 02:19:02Z 位于区间内而非其后，首条 Run 响应未保留；8 条 per-run 仅 3 条有身份、4 个调查 Run 无身份字段；B2 的 wall-time 实际只取材 M002。四处表述已更正并标注结论依据退役公告而非身份覆盖。**实质结论（不重新校准）不变** |

| R3 | `live.py` 的 `token_usage()` 识别集含虚构 id `deepseek-v4.1-flash`、缺真实回报名 `deepseek-flash`，且在准入校验前逐响应调用 | **采纳** | 切换后每条真实响应会被记成 `unreported_or_unrecognized`。已补 `deepseek-flash`/`DeepSeek-Flash`、移除虚构 id、加注释区分账本识别与准入校验 |
| R3b | `docs/development.md` 的操作指引仍写 `reported_alias=deepseek-v4-flash` | **采纳** | 已更新为 `deepseek-flash` 并注明旧名对应模型已退役 |
| R3c | 「原先的双名允许集」前提对 `live.py` 不成立，该文件从来是单值 | **采纳** | 双名允许集在 `round02/03.py` 与 provider-identity 决定文档，是另一套机制。已更正表述，并如实说明本次顺带修复了 `live.py` 对真实端点的必然失败 |
| R5 | 任务记录把未合并分支上的合同提案当作变更依据引用 | **采纳** | 改为引用用户 2026-09-15 直接决定；提案（PR #24）另行提及并标注尚未合并 |

## 下一步与交接

1. **真实调用验证仍未做**：需一次有界真实请求确认 `deepseek-flash` 在官方端点可用且回报同名。
   在此之前，SPEC 中该 profile 的「availability 与协议兼容性需 M0 验证」条款仍然成立。
2. 独立审查：本任务涉及权威来源（SPEC/C3）变更与 fail-closed 校验收紧，需独立审查后再提 PR。
3. [合同提案 PR #24](https://github.com/kevinWangSheng/production-ops-agent/pull/24) 的 §5 U1
   已由本决定裁定；该 PR 合并前后需同步把 U1 标记为已决，避免留下未决项的陈旧表述。
4. 本任务未启动任何进程或服务；worktree 在 PR 合并且确认整合后清理。
