# ITBench-Lite 全量：35 题 × 3 次（#571）

类别：集成运行（真实 DeepSeek 调查、回放组件、真实 judge），单边结果（只跑 OpsPilot，HolmesGPT 对照另补），只代表 Lite 轨道，与现场版分开报。依据 [租机手册](../../tasks/2026-11-13-rental-runbook.md) 阶段 6；主机、提交 F 与冻结版本见 [租机记录](../rental-2026-11/run.md)，冒烟与小批见 [冒烟](../itbench-lite-sre-lite-smoke-1114b/run.md)、[小批](../itbench-lite-sre-lite-small-1114/run.md)。时间 UTC。

**换算在全量跑完后修过，调查没有重跑。** 原始轮次的换算把命名空间名当成了根因实体（#638，见下）。修复后用冻结的 105 份 summary 离线重新换算、重新判分；报告与证据不变，主分数以修复后的为准，原始分并列保留。

## 时间线

| 时刻 | 事件 |
|---|---|
| 2026-11-15T02:13:00Z | 全量开跑：`driver round --round lite-full-1114 --dataset <数据集根> --vocabulary-dir tmp/itbench-lite/inputs/vocab --env-file ~/rental-private/lite.env`，主 checkout 在提交 F `5f731318` |
| 2026-11-15T14:11:54Z | `DRIVER_EXIT=0` |
| 之后 | 补判退出码 0；4 张原始分数表写出；发现 #638 |
| 2026-11-15T16:07:21Z | #638 修复（`a720b9d8`）后离线重算 `rescore --into lite-full-1114-r638`，退出码 0 |
| 2026-11-15T16:08:24Z | 重算轮次的 judge 开始 |
| 2026-11-15T18:25:05Z | 重算轮次 judge 结束，`JUDGE_EXIT=0`：105 个 `ok`、全部 `judged: true`；`scores` 输出 `scenarios: 35` |

## 原始轮次检查（lead 核对）

| 判定项 | 结果 |
|---|---|
| 105 个 `summary.json`，`status` | 105/105 `completed`，缺失 0 |
| 版本与提交（`jq … \| sort \| uniq -c`） | 只有一行：`prompt-replay-candidate-k8s-0282ee7d0e7a` / `otel-demo-ext-d467c4b436b0` / `5f731318` / dirty `false` |
| 作废与基础设施失败（`itbench_lite.history`） | 全空：作废 0，基础设施失败 0 |
| judge | 105 个 ok；补判退出码 0 |
| 结束后端口与 `itbl-*` 容器 | 0 |

补充（本记录从 summary 统计）：灌数据 47–106 秒，数据末尾到投递 112–171 秒；LangSmith 回读 105/105 `found`（每个 summary 的 `trace.roots[].url`，项目 `opspilot-lab-itbench-lite-lite-full-1114`）；结论 `supported` 70、`partial` 35；Run 终态 `completed` 104、`waiting_human` 1（S23 trial-1，有报告）；换算 `reason` 全为空（没有报告校验失败）。

## #638：命名空间名被当成根因实体

- 现象：原始轮次 105 份 `conversion_meta.json` 里，58 次的 `root_cause_entity` 形如 `otel-demo/<Kind>/otel-demo`，39 次为空，8 次是真实对象；105 份答案输出共有 99 个以 `/otel-demo` 结尾的实体。确定性分 `det_*` 105 次全为 0。
- 原因：快照里有一个与命名空间同名的 ServiceAccount，按词表规则 L-V6 收进了每题词表；报告总结常写 `otel-demo/<服务>`，换算取总结里第一个词表命中名作根因（L2），于是命中命名空间名。
- 修复（#639，`a720b9d8`）：换算不再把部署命名空间名当实体候选；其他换算规则不变，没有按分数调整。
- 重算：在主机上另一份 checkout（`~/poa-638-dry`，`a720b9d8`）里跑，主 checkout 保持在 F `5f731318` 不动，便于对照 summary 记的提交。命令（模块都用主 checkout 的 `.venv/bin/python`）：
  - `driver rescore --round lite-full-1114 --into lite-full-1114-r638 --vocabulary-dir <主 checkout>/tmp/itbench-lite/inputs/vocab --tmp-root <主 checkout>/tmp/itbench-lite --evidence-root <主 checkout>/docs/evidence --into-tmp-root ~/poa-638-dry/tmp/itbench-lite`：105/105 `converted`，退出码 0。
  - `set -a; . ~/rental-private/lite.env; set +a`，`ITBENCH_EVALUATIONS_DIR=$HOME/rental-data/ITBench-Evaluations`，`driver judge --round lite-full-1114-r638 --ground-truth <数据集根> --tmp-root ~/poa-638-dry/tmp/itbench-lite`
  - `driver scores --round lite-full-1114-r638 --tmp-root ~/poa-638-dry/tmp/itbench-lite --out …/scores.json`，再 `coverage --tiers tmp/itbench-lite/inputs/tiers/tiers.json` 出 4 张表，放在 `rescore-r638/`。
- 重算后的根因形态：真实对象 33（Service 31、Pod 1、Deployment 1），空 72，以 `/otel-demo` 结尾 0；答案输出里以 `/otel-demo` 结尾的实体 0。空的 72 次都来自既有规则 L2：70 次总结里第一个命中名也出现在 `rejected_hypothesis` 主张里，2 次总结里没有词表命中名；结论为 `inconclusive` 的 0 次。本轮没有改 L2。

## 分数

主分取根因实体 F1 与官方 pass@1（L7）。没有报告或缺失的 Run 计 0、留在分母（L5），35 题全在分母里。

| 指标 | 档位 | 原始（`lite-full-1114`） | 修复后（`lite-full-1114-r638`） |
|---|---|---|---|
| 官方 pass@1 `root_cause_entity_pass` | 指标+trace（14 题） | 0.071 | 0.000 |
| | 故障窗无 trace（21 题） | 0.063 | 0.048 |
| | **全部（35 题）** | **0.067**（7/105） | **0.029**（3/105） |
| 官方根因实体 F1 `root_cause_entity_f1` | 指标+trace | 0.071 | 0.000 |
| | 故障窗无 trace | 0.063 | 0.048 |
| | **全部** | **0.067** | **0.029** |
| 确定性 `det_f1` | 指标+trace | 0.000 | 0.000 |
| | 故障窗无 trace | 0.000 | 0.032 |
| | 全部 | 0.000 | 0.019 |
| 确定性 `det_pass` | 指标+trace | 0.000 | 0.000 |
| | 故障窗无 trace | 0.000 | 0.032 |
| | 全部 | 0.000 | 0.019（2/105） |

修复后官方分低于原始分，确定性分从 0 升到 0.019。两套分数照实并列，没有拿任何一套去调换算规则；每题明细见各表。

表文件：原始 `table-<指标>.{json,md}`，修复后 `rescore-r638/table-<指标>.{json,md}`；分档 `tiers.json` 与明细 `tiers_detail.json`。judge 是 Z.ai `glm-5.3-flashx`（L6 = a），**judge 与官方示例不同**。

## 费用

- DeepSeek（调查）：105 个 Run 冻结 summary 的 `run.tokens` 合计：缓存命中 126,003,840、未命中 11,591,095、输出 4,205,704（2,225 个带用量的模型轮次）。全程在北京时间 2026-11-15（周日）10:13–22:12，全部闲时。单价取仓库已有记录 [soak 费用主数](../m3-soak-2026-11-07/cost-primary.json)（`deepseek-flash` 官方价格页，2026-11-07 读取，闲时每百万 token 命中 0.02 / 未命中 1 / 输出 4 CNY；本轮当天没有重新读取）。合计 **CNY 30.93**（命中 2.52 + 未命中 11.59 + 输出 16.82）；按高峰价算的上限 CNY 61.87。
- Z.ai judge：评测器输出不带用量，未计价。调用次数：原始轮次每 trial 至少 1 次（105 次，含重试未统计），重算轮次 105 次。
- 重算不调 DeepSeek。

## 已知限制

- 根因由启发式 L2 得出（总结里第一个词表命中名，被 `rejected_hypothesis` 提到的不算）；传播链只抽总结里的显式箭头，相关分数受转换限制（L3）。
- `RESULT_TOO_LARGE`：105 个 Run 里 80 个至少遇到一次（主要是 `traces_search`、`traces_search_by_tags`）；`INVALID_PARAMS` 49 个 Run（主要是 `kubernetes_jq_query`）。全部 4,228 次工具请求。
- 回放 Prometheus 不带告警与录制规则，`list_prometheus_rules` 只能返回空。
- S12、S33 的入口告警没有 `service_name`，按回退规则跑（`target_fallback: true`，各 3 次）。
- S18、S38 快照数据残缺，照常计入分母并在表里单独标出。
- 日志与 trace 覆盖按 `tiers.json` 分档：指标+trace 14 题，故障窗无 trace 21 题。
- 本轮只跑 OpsPilot；judge 与官方示例不同，分数不能直接与官方榜单比较。

## 入库与核对

入库：105 个 `S<id>/trial-<n>/summary.json`、原始与修复后各 4 张分数表、`tiers.json` 与 `tiers_detail.json`、本文件。不入库：`tmp/itbench-lite/` 下的答案输出、`conversion_meta.json`、分数文件、评测器工作目录、原始 ledger；任何答案文件。复制前在主机上对 k8s token 与 `~/rental-private/known-secrets.txt` 的 11 个值逐个原文搜索（共 12 个值），0 命中；同一脚本放入一个已知值做正对照，1 命中；本地解包后 sha256 与主机清单逐个比对，123 个文件全部一致。主机未销毁。
