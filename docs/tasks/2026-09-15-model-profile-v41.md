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

- 前提：核对官方文档与仓库证据；真实模型调用一次（用户 2026-09-15 明确授权），实际用量 37+64 tokens、低于 0.001 CNY。
- 完成条件：前瞻性配置全部切换；`make check` 通过；权威来源（SPEC、C3）记录决定与依据；
  冻结包的原文保留并加注；ROADMAP 反映实际状态；独立审查完成且发现已处置。

## 改与不改的划分

**已改（前瞻性——决定下次真的调用什么）**

| 文件 | 内容 |
|---|---|
| `scripts/m0/config.py` | `PROFILE["OPSPILOT_MODEL"]` |
| `.env.example` | `OPSPILOT_MODEL` |
| `scripts/m0/live.py` | `MODEL_PROFILE` 的 `request_model` 与 `accepted_response_model` |
| `scripts/m0/adapters.py`、`scripts/m0/protocol.py` | 合成排演的 `create(model=...)`。**两者都不是真实出站**（`no_network()` + `base_url="https://model.invalid"`），改动目的是让排演跟随当前 profile、保持代表性。唯一真实出站路径是 `live.py`。初稿把 `adapters.py` 归为「决定下次真的调用什么」是错的，且漏了同类的 `protocol.py`（独立审查发现） |
| `tests/test_m0_live.py` | 响应名合同测试的正常分支替身值 |

**已改（权威来源记录决定）**：`SPEC.md`、`docs/design/technical-proposal-2026-09-07.md`、`ROADMAP.md`。
另 `docs/development.md` 的操作指引与 `docs/plans/delivery-and-resources-2026-09-08.md` 的现在时断言已同步/加注。

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

**已执行：一次有界真实调用（2026-09-15，用户授权）。**
请求 `deepseek-flash` → HTTP 200、回报名 `deepseek-flash`、0.94 s、37 input / 64 output tokens。
证据见 [`probe-2026-09-15.md`](../evidence/m0-model-profile-v41/probe-2026-09-15.md)。

判定：`accepted_response_model = "deepseek-flash"` 的精确匹配 fail-closed **取值正确**，
不会误拒真实响应——这是本次切换唯一先前未被证据定死的点，现已定死。

附带观察：`finish_reason=length`，64 个 completion tokens 全部是 `reasoning_tokens`，正文为空。
thinking enabled/high 下 `max_tokens` 必须覆盖推理预算加正文，否则得到空正文；
这复现了历史记录里的「length 空正文」，属参数配置问题而非模型故障。

用量记账：37 input / 64 output，按峰值上界保守估计低于 0.001 CNY；供应商账单未对账，
ROADMAP B8 状态不变。

**仍未验证**：单次成功不证明协议/恢复矩阵、多轮工具调用、流式续接或并发行为；
M0 退出条件与产品验收均不因本次探针改变。

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
| R4 | `deepseek-flash` 是浮动别名，精确匹配更严但**分辨力更低**，无法再检测代次更替 | **采纳** | SPEC/C3 补 caveat。`version_scope` 语义调整初列为开放项，后由机器人 P1 推动落实，见下行 |
| 机器人 P1（三轮） | 合同层只校验格式；`execute()` 不取 `/models`、不比对，**也没把该值写进 Run 记录**——陈旧或伪造的摘要仍能放行 | **采纳** | 最后半句是我的错：上一轮写了「该摘要随 Run 记录」，实际只有 `contract_hash` 覆盖它（可证明未被篡改，**无法回读取值**）。已加可读列 `m0_live_once.models_metadata_sha256`，`claim()` 写入，并按既有模式加旧 schema 守卫（缺列即拒绝，须重建 lab 库）。代码注释、SPEC、C3、development.md 的表述一并更正。**取/models 并比对仍未做**，理由不变：须多一次调用与授权 |
| 机器人 P2（三轮） | SPEC 段落仍写 `version_scope` 为 `reported_alias`，紧接着又说记为 `floating_alias`，自相矛盾 | **采纳** | 上一轮改了 C3 漏了 SPEC，已更正并去掉重复句 |
| 机器人 P2（二轮·账本大小写） | 已登记 id 的其它大小写会退化成 `unreported_or_unrecognized` | **部分采纳** | 采纳实质：改为小写规范登记 + 不区分大小写比对。**拒绝两个建议修法**：不加 `DeepSeek-Flash` 字面量（未经观测的臆造变体，与冻结包「仅使用已有官方 metadata 证据」冲突，且正是上一轮被删的那条）；不采纳 preserve raw value（未登记值压平是既有的有意边界，不让 provider 文本穿透账本）|
| 机器人 P1（二轮） | 改 `version_scope` 只是换标签；`validate()` 仍接受该 profile，`execute()` 不做任何 `/models` 核对，代次更替后仍能通过名称校验并与冻结校准比较 | **采纳** | 反驳成立：文档化的前置条件没有任何东西强制。已在**合同层加强制闸**——`models_metadata_sha256` 列为必填字段，须为 64 位小写十六进制，缺失或格式非法在 claim 前 `denied()`；该摘要随 Run 记录。零新增外呼：`/models` 调用发生在批准准备阶段（round02/round03 已有同一机制）。补 3 个拒绝用例（空值、非法格式、大写十六进制）。**仍开放**：运行时无法在不发起额外请求的前提下证明该摘要是当前值，新鲜度仍由批准方承担，已如实记录 |
| 机器人 P2（二轮） | C3 仍写 `version_scope` 为 `reported_alias` | **采纳** | 已改为 `floating_alias`，并说明该取值如实反映更弱的保证 |
| 机器人 P1 | 浮动别名 + `version_scope` 仍写 `reported_alias`，未来每一代都能通过名称校验并被记成同一模型，可能与冻结校准静默跑在不同后端上 | **采纳** | 三处落实：(1) `version_scope` 改为 **`floating_alias`**，如实记录更弱的保证，不再高估；(2) SPEC/C3 把漂移检测从 caveat 升级为**前置条件**——任何 Run 与冻结校准比较前须先用 `/models` 元数据核对确立后端身份，该机制仓库已有（`provider_models_response_sha256`）；(3) 在 live profile 内携带已批准的 models 元数据 hash 列为开放项，因其使每次批准多一次调用，须另有决定与授权，不自行实施 |
| R4b | B6 的 runner 仍钉已退役名，改与不改都有代价 | **采纳（仅记录）** | 列入「未决开放项」，不静默编辑 runner |
| R4c | `adapters.py` 被归为「真实出站」实为合成排演；同类 `protocol.py` 未归类 | **采纳** | 两者都在 `no_network()` + `model.invalid` 下运行。已更正分类、同步 `protocol.py`，并写明唯一真实出站是 `live.py` |
| R4d | `docs/plans/delivery-and-resources-2026-09-08.md` 的现在时断言未更新 | **采纳** | 已加注被 2026-09-15 决定取代 |
| R5 | 任务记录把未合并分支上的合同提案当作变更依据引用 | **采纳** | 改为引用用户 2026-09-15 直接决定；提案（PR #24）另行提及并标注尚未合并 |

## 未决开放项（本任务不处理，需单独决定）

**B6 的 runner 仍钉已退役名。** `scripts/m0_environment/holmes_baseline.py`、
`scripts/m0_lab/round07/upstream_runner.py`、`candidate_runner.py` 都发 `deepseek-v4-flash`。
ROADMAP 记 B6 为**部分完成**，「正式可比报告、人工校准和盲测仍未完成」。
于是形成两难：改 runner 会破坏与已记录 M004 runs 的可比性；不改则 B6 只能用已退役名完成。
**本任务只记录该开放项，不静默编辑 runner**——选哪条属实验设计决定，须单独立项。

**models 元数据摘要的新鲜度。** 合同层已强制声明 `models_metadata_sha256`
（缺失或格式非法即拒绝），并由 `claim()` 写入可读列 `m0_live_once.models_metadata_sha256`
供事后核对；但**运行时无法证明该摘要是当前值**——
证明它需要在 claim 时多发一次 `/models` 请求，涉及费用与授权，须单独决定。
在此之前，摘要的新鲜度由批准方承担：准备批准合同时须实际调用 `/models` 并取其摘要，
不得沿用旧值。这一点须在下一次真实实验的合同准备中明确执行。

**lab schema 需重建。** `m0_live_once` 新增 `models_metadata_sha256 NOT NULL` 列，
`claim()` 按既有模式加了守卫（`SELECT models_metadata_sha256 ... LIMIT 0`），
缺该列的旧 lab 库会在消费授权和发起 HTTP 之前被拒绝。
下次真实实验前须重建隔离 lab 库，不做原地迁移（与 `live.sql` 头部
「Explicit isolated experimental setup only; no product or existing synthetic table migration」一致）。

## 下一步与交接

1. ~~真实调用验证~~ **已完成**（见上）。SPEC 中「协议兼容性需 M0 验证」的其余部分
   （多轮工具、流式、恢复矩阵）仍然成立。
2. **既有 v2 批准合同文件须重发**：`live.py:113` 对 `contract["model_profile"]` 做整字典相等比较，
   任何仍写旧 `request_model`/`accepted_response_model` 的合同文件会直接得到
   `LIVE_MODEL_PROFILE_MISMATCH`。这是下次真实运行第一个会撞上的东西。
3. 独立审查：已完成三批，发现见上表。
4. [合同提案 PR #24](https://github.com/kevinWangSheng/production-ops-agent/pull/24) 的 §5 U1
   已由本决定裁定；该 PR 合并前后需同步把 U1 标记为已决，避免留下未决项的陈旧表述。
5. 本任务未启动任何进程或服务；worktree 在 PR 合并且确认整合后清理。
