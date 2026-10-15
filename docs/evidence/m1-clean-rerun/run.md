# M1 干净重跑记录（2026-10-15，#247）

- 目的：在 [M1 退出端到端运行](../m1-exit-e2e/run.md) 恢复未确认（宿主内存干扰）之后，从空库再跑一次完整路径并拿到确认恢复；同一环境再注入一次故障，为 #238（F13 第 4 步，知识线索进入调查输入）和 #246（F12 PR-1，结论后追问开新 Run 并携带先前报告）提供真实 Run 证据。
- 运行代码：本地集成分支 `integ/m1-clean-rerun` `0b09559`（`main` 含 #239/#241，加 #238 与 #246 合并、冲突按 F12 r2 解决），原样保留在远端分支 `chore/m1-clean-rerun-integ`（不合并，供核对运行代码）；`make check` 7561 passed / 0 failed（同一提交）。摘要 `versions.executed_commit` 记的就是这个提交。
- 冻结摘要：[run/summary.json](run/summary.json)（阶段 A，`frozen_at` 18:41:13Z，sha256 `48c269d2…c2b5c9`）；批准记录 [run/approve.json](run/approve.json)；阶段 B 冻结摘要 [run-b/summary.json](run-b/summary.json)（事故 `ae7a0387`，Run `49a537d7` 与 `3ed23c95`，`frozen_at` 19:20:05Z，sha256 `b1e38b47…4c96a3c`，trace 根 run 2 个；collector 只能填 `--scenario S1`，阶段 B 未作恢复判定，其恢复字段为未确认，按此读）；阶段 B 只读回读 [run/phase-b.json](run/phase-b.json) 补摘要不含的 Run 输入摘录（知识条目名、先前报告、各 Run 输入 sha256）与追问响应。原始 ledger 不入库：阶段 A 本机 `tmp/lab-ledgers/m1-clean-rerun/2c321376-…/ledger.json`（sha256 `e55c9e4b…855156`，1,248,877 B）；阶段 B `…/3ed23c95-…/ledger.json`（sha256 `f8b0433e…b84e4aa`，893,850 B，含两次 Run 的输入，即知识条目与 `Conclusion: partial` 先前报告）。阶段 B 取证时只起 PG 与 web（`OPSPILOT_SELF_HEAL_INTERVAL_SECONDS=0`，不再写自动授权），worker 与 Observer 已停，观察会话停在 `authorized`。
- 判据：[verify.py](verify.py) 与 [m1-exit-e2e/verify.py](../m1-exit-e2e/verify.py) 逐字相同；[collect.py](collect.py) 只改了实验名、round 与输出子目录参数 `--evidence-subdir`；[approve.py](approve.py) 同上次；[followup.py](followup.py) 以工作台审核人身份提交一次追问表单（凭据读文件，不打印）。

## 环境

- 新起 PostgreSQL 17（127.0.0.1:55592，库 `m0_budget`），空库 `opspilot.schema migrate` 到 `0017_web_suspension_grants`；web/worker 用 `postgres_lab runtime-roles` 建的非 owner 角色，Observer 用独立登录角色。告警投递 `delivery_id` 从 1 开始。
- kind + OTel Demo 0.37.8；开跑前 `kind_lab.py health` 全部 ok、所有 flag off。为加快告警与稳定 p95，运行中把 load-generator 的 `LOCUST_USERS` 从 2 临时调到 6（只改实验集群上的 Deployment，`values.yaml` 不变，checkout 请求率约 0.07 req/s）。
- 运行期间未并行跑 `make check`、全量 pytest 或 Codex；宿主 swap 仍在 15.0–15.3 / 16.4 GB（未冻结，lead 观测）。

## 阶段 A：告警 → 调查 → 自动恢复观察 → 复盘 → 批准

| 时间（UTC） | 事件 |
|---|---|
| 18:00:43 | 注入 `paymentFailure` 100%（实验 `m1-clean-20261015`，摘要 `lab_actions`） |
| 18:07:53 | Alertmanager 投递 1 `firing` → 建事故 `002c3fba` 与 Run `2c321376` |
| 18:09:21 | 结论发布（`supported`，预算计数 6、工具调用 14 次） |
| 18:09:25 | 自动授权观察会话（来源 `automatic`，`system`）；18:09:31 恢复故障 |
| 18:10–18:13 | 样本 1–4 `degraded`（5 分钟窗口仍含故障期，error_ratio 0.375 → 0.167） |
| 18:14:28 | 样本 5 起 `healthy`，之后连续健康 |
| 18:24:37 | `recovery_confirmed`，健康窗 602 s，15 个样本，事故 `resolved` |
| 18:24:54–18:25:54 | 复盘生成 1 次尝试成功（1 次模型请求，输入 35,389 tokens） |
| 18:26:08 | 工作台批准版本 1（HTTP 200），发布 1 条知识 revision |

verify.py S1：**11/11 通过**（C1–C7、S1-1..4；上次 7/11，S1 四项因恢复未确认失败）。

## 阶段 B：知识线索进入调查（#238）与结论后追问（#246）

| 时间（UTC） | 事件 |
|---|---|
| 18:26:28 | 再次注入（实验 `m1-clean-20261015-b`；同一实验 id 拒绝重复注入） |
| 18:33:56 | 新事故 `ae7a0387`、Run `49a537d7`：输入 `scope_facts.knowledge` 状态 `ok`，1 条，即阶段 A 批准的「Checkout PlaceOrder error ratio at 100% while only payment calls fail」 |
| 18:35:06 | 结论发布 |
| 18:35:13 | 工作台追问（`e2e-lab`）（`follow_up`，HTTP 200，代际 0→1）→ 新 Run `3ed23c95` 排队；旧 Run 保持 `completed`；事故仍 `open` |
| 18:35:16–18:39:58 | 新 Run 输入含先前报告（`Conclusion: partial` + `Summary: …`）与同一知识条目；跑完发布新结论 |
| 18:40:05 | 恢复故障 |

阶段 A 的 Run 输入同样带 `knowledge`（状态 `ok`、0 条，当时库里还没有知识），即线索为空时也照常出现、不阻塞调查。阶段 B 只取 #238/#246 的输入与 trace 证据，不作恢复判定。

## trace（project `opspilot-lab-m1-clean-20261015`，保留期 longlived）

| 根 run | 关联 | 链接 |
|---|---|---|
| `opspilot.run` | 阶段 A 调查 Run `2c321376` | [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/938480c0-652b-4ee9-a771-a70bbc56fe16/r/00000000-0000-0000-9c4b-dbb80c662fd9?poll=true) |
| `opspilot.postmortem_generation` | 阶段 A 复盘尝试 `6ae9f154` | [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/938480c0-652b-4ee9-a771-a70bbc56fe16/r/00000000-0000-0000-37d5-9882c1f94ca8?poll=true) |
| `opspilot.run` | 阶段 B 首个 Run `49a537d7`（带知识线索） | [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/938480c0-652b-4ee9-a771-a70bbc56fe16/r/00000000-0000-0000-9bd6-c9199a677c4e?poll=true) |
| `opspilot.run` | 阶段 B 追问后的 Run `3ed23c95`（带先前报告） | [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/938480c0-652b-4ee9-a771-a70bbc56fe16/r/00000000-0000-0000-63e7-d99a6baa5727?poll=true) |

## 费用

DeepSeek 余额 204.70 → 204.20 CNY，共 0.50 CNY（赠送余额扣减，充值余额 200.02 不变），覆盖两个阶段全部模型调用。

## 限制

- 运行代码是本地集成提交，不是 `main`；#238、#246 合并后 `main` 的内容与之相同（squash 后按内容核对）。
- load-generator 用户数从 2 调到 6 是实验参数，与上次不同；恢复判定的阈值与窗口未变。
- 这是 kind 实验环境上的有界运行，不是生产证明。
