# S7c 配对排程、多 lab 并发与 dataset 前缀

- 状态：进行中（实现与离线测试完成；HolmesGPT 侧 runner 接口待 S7 Holmes 线对接）
- 更新日期：2026-11-04
- 依据：F1 收尾裁决 `~/.agent/handoffs/opspilot-rulings-2026-11-04-f1-closure.md`（07:40 PT 修订优先）；预审 `opspilot-prereview-f1-closure-plan-2026-11-04.md` R2、R7、R8、r0.1-3、§3.3
- 工作区：`/Users/shenghuikevin/dev/AI/poa-s7-schedule`，分支 `feature/F1-s7-paired-schedule`；多实例 lab 另在 `/Users/shenghuikevin/dev/AI/poa-s7-labinst`，分支 `chore/kind-lab-instances`（基于 #462，等 #462 合并后单独开 PR，不做 stacked PR）

## 目标与范围

1. R7 配对排程：同一次注入、同一窗口内 OpsPilot 与 HolmesGPT 先后跑，先后轮换；同题两次间隔 ≥60 min，洗净 ≥600 s；每槽记 `predecessor` 与 `contamination`；每题 3 次。
2. 并发：题目分到 N 个相互独立的 lab 并行跑，每个 lab 同时只跑一个窗口。
3. R8：`dataset` 维度改为 `<数据集名>:<tree hash>`。
4. 出题前预注册作废/重跑规则。

## 做了什么

- 预注册规则：`evals/preregistered/f1-closure-paired-rules.json`（`f1-closure-paired-r1`）。四类作废：未建 Run 的环境 setup 失败、模型供应商 5xx（排除注入 503 的题）、共享机噪声（steal>5% 或基线 p95>2×）、harness 中断；每窗口最多重跑一次，整窗口（两系统）一起重跑，原次保留；重跑结果照实计分。代码只接受它实现了的规则，规则文件改了不兼容的字段会被拒（`RULES_UNSUPPORTED`）。
- `scripts/eval_schedule.py`：`plan`（离线模拟，给顺序和预计时长）、`run`（每 lab 一个线程，共享同一个调度器，经 ssh 在各 lab 上逐窗口调 `scripts.eval_live`，每完成一个窗口重写日志）、`check`（按规则检查日志：重叠、洗净不足、同题间隔不足、顺序不符、无依据重跑、漏跑）。
- `scripts/eval_live.py --pair opspilot-first|holmes-first|opspilot-only`：一个 round 一个窗口；一次注入下先后跑两系统；第二个系统开跑前重新确认故障（或正常窗口）与噪声，并重写合成 span，保证两边拿到同样新鲜的证据；重确认失败只算第二个系统 setup 失败。每个 attempt 写 `runs/<attempt>/pair.json`（每槽 system、position、status、reason、run_created、provider_error、host_noise）。
- R8：`dataset_name()` 取数据集根目录名，`dev-v2` 结果不变；不合规名字拒绝（`DATASET_NAME_INVALID`）。`eval_dev_round` 同步。
- 多实例 lab（另一分支）：`OPSPILOT_LAB_INSTANCE=n`（0–3）派生集群名、LAB_DIR、Prometheus 账号文件、控制侧别名与 host 端口（+100n）；实例 0 与原来完全一致；colima 下拒绝 n>0。`eval_live_inject` 的 OTLP/Jaeger 地址跟随实例。

## 可逆默认（Agent 自定，一行理由）

| 编号 | 默认 | 理由 |
|---|---|---|
| D1 | 同题 ≥60 min 的间隔按 lab 计，不跨 lab | 间隔的目的是防残留痕迹（#432 同题痕迹 6 槽）；两个 kind 集群不共享 Prometheus/Jaeger |
| D2 | 洗净 600 s 由调度保证：同 lab 上一窗口恢复正常到下一次注入 ≥600 s；eval_live 原有的干净窗口检查照旧 | 原 wash 是 300 s 干净窗口、600 s 上限，不等于 ≥600 s |
| D3 | 轮换：第 i 题第 r 次在 i+r 为偶数时 OpsPilot 先 | 每题交替，相邻题起点相反，dev-v2 先手 22:20 |
| D4 | 调度：lab 空闲时取可开跑的窗口中重复序号最小、数据集序最前的；重跑优先 | 先把每题第 1 次跑完，同题自然拉开间隔 |
| D5 | 第二个系统前重确认（最近 240 s 内签名或 300 s 正常窗口）并重写合成 span | 先手 Holmes 约 3–8 min，OpsPilot 的 300 s 提交新鲜度会过期；prompt-injection 题的 span 也会滑出 5 min 窗口 |
| D6 | 作废按窗口整体重跑 | 保持两系统对称，与 r0.1-3「整对重跑」一致 |
| D7 | 污染标记照记不作废，报告时另给剔除标记槽的敏感性行 | A11/A12 沿用；作废会让结果依赖事后判断 |
| D8 | labs 文件禁止带凭据类变量名；同主机两 lab 不得共用 PG 端口、输出目录、ledger 目录或实例号 | 凭据由用户命令送上主机；共用即同一个 lab |

## 证据

- 离线：`tests/unit/test_s7_schedule_impl.py`、`tests/unit/test_s7_paired_live_impl.py` 44 passed；相关驱动与 #431/#432 验收及合同测试 1403 passed（本机定向跑）；多实例 `tests/test_kind_lab_instances.py` 等 104 passed。
- 主机实测（opspilot-f9-1，2026-11-04 08:36–08:46 PT，只起 lab，未开故障开关、未放 key、未调模型）：在 S4 的 `opspilot-m1` 运行时，用实例 1、2 各起一个 lab（每个约 4 min），三个 lab 的 `health` 同时 ok。内存 used 2.9 GB → 4.7 GB（2 个 lab）→ 6.2 GB（3 个 lab），available 9.8 GB，load 约 1；节点容器各 2.0–2.6 GiB。测完删除实例 1、2，回到 2.8 GB。
- 估算（dev-v2 18 题 × 3，窗口 27 min + 洗净 10 min）：1 lab 33.1 h，2 lab 16.5 h，3 lab 10.9 h。

## 待决与风险

- HolmesGPT runner：`eval_live` 按 `BaselineRequest -> BaselineResult` 调 `scripts.holmes_baseline.dataset_runner.paired_runner(deps)`，等 S7 Holmes 线确认接口；其 relay/egress 容器名与网络名是固定的，多实例时需按 `kind_lab.SUFFIX` 区分（已告知）。
- 并发与 D15「同一时间只跑一个评测」：本实现的读法是每个 lab 同时一个窗口。多 lab 共享同一台主机的 CPU，延迟类信号会互相影响，但两系统在同一窗口内先后跑，影响对两边对称。是否接受这种读法由 lead 定。
- 推荐 lab 数：16 GB 主机上 3 个（实测 3 个 lab 用 6.2 GB；每个 lab 另加 Holmes 容器 1 GB 与 harness/PG，推断峰值约 12 GB）；4 个不推荐。
- 未实现：每窗口基线 checkout p95 的采集（`host_noise.baseline_p95_ratio` 恒为 null）；共享机分支（U7）启用前需补。HolmesGPT 槽的 `contamination` 需要 Holmes 的工具输出视图，由事后汇总处理。
- 汇总：`eval_dev_round` 仍按每题 2 槽（#432）；k=3 与配对汇总由判分线（S7 judge）负责。
