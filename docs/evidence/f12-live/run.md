# F12 真实运行记录（2026-10-16，PR-3 证据）

- 目的：在 kind 实验环境用真实告警、真实模型与常驻 web/worker 走通 F12 四步，作为 [F12 合同](../../tasks/2026-10-15-f12-contract.md)第六节「真实运行」的证据。
- 运行代码：`f497424ab82bea87707c61cca3a544ee5b215b97`（本地分支 `test/F12-acceptance`）= #252 head `266a8ef`（产品代码同 `ab79c55`）+ 独立验收测试提交。确定性验收 `tests/integration/test_f12_acceptance_postgres.py` 在同一提交 14 passed。
- 冻结摘要：[run/summary.json](run/summary.json)（sha256 前缀 `59f02adb0ba39f2a`），含逐步检查、页面 sha256、14 个 trace 根链接与余额；判据由 [collect.py](collect.py) 从产品行与运行中保存的页面只读计算。原始 ledger 不入库：本机 `tmp/lab-ledgers/f12-live/e1ac6e74-…/ledger.json`（sha256 `1296a2a6…f6e4`，1,551,986 B，含页面 HTML 与每次控制的请求前后状态）。
- 操作工具：[ctl.py](ctl.py) 以工作台审核人（Basic Auth，`e2e-lab`）身份向 `POST /incidents/{id}/control` 提交与页面表单相同的字段（带 Origin），并保存页面 HTML；[redeliver.py](redeliver.py) 做告警手工重投。凭据读文件，不打印。

## 环境

- PostgreSQL 17：复制 `tmp/m1-clean/pg`（M1 干净重跑库，含两条旧事故）后在 55593 启动；web/worker 用非 owner 运行角色。未启 Observer（F12 四步不依赖采样；观察会话的授权与撤销由 web 事务完成）。
- kind + OTel Demo；开跑前 `kind_lab.py health` ok，checkout p95 首测 310 ms，2 分钟后复测 189 ms、rate 0.0375 req/s 后注入；swap 15.4–15.9 / 16 GB；同机另有其他项目在跑 pytest（不受本线控制）。load-generator 用户数运行中 2→6、结束后回到 2。web 在 8080（Alertmanager webhook 固定指向 8080，协调者批准）。
- 场景 B 注入前未等基线回落（p95 1805 ms、错误率 0.31 为场景 A 故障窗口的 5 分钟残留）；场景 B 只检验控制语义，不依赖延迟。

## 场景 A：事故 `e1ac6e74`（故障 `f12-live-20261016`，08:07:18 注入 → 08:29:59 恢复）

| 时间（UTC） | 动作 → 结果 | 步骤 |
|---|---|---|
| 08:13:04 | Alertmanager 投递 `firing` → 事故与 Run `bb64bec6`；08:16 发布结论；证据链接以 HTML 打开（200） | 1 |
| 08:17:03 | 结论后 `follow_up`（查 payments 依赖）→ 200、代际 1、新 Run `9e760c03`；旧 Run 保持 completed；08:19 新结论，页面「Investigation history」含旧 Run | 1 |
| 08:24:50 | `correct` 第二份结论中的假设（错误来自 flagd 开关，不是 Charge 代码缺陷）→ 新 Run `c7d8b699`，输入带先前结论并标「not instructions」；审计行与输入行含原文 | 2 |
| 08:25:02 / 08:25:08 | 该 Run 运行中 `pause`（徽章 paused、Run paused）→ `resume`（重新排队）；08:27:40 发布更正后的结论 | 3、2 |
| 08:27:53 / 08:28:04 | `follow_up` 开 Run `dc25871c` → `cancel`（Run 与事故 cancelled） | 3 |
| 08:28:12 / 08:28:37 | 取消后 `follow_up` 开 Run `1f53e856`（running）→ `takeover`：mode human_owned、Run waiting_human | 3、4 |
| 08:28:46 | 接管前派出的 worker 写回第 2 轮 → `late_result`（仅历史，worker 记 `CONTROL_DENIED`），无发布 | 4① |
| 08:29:09 | 手工重投同一 firing 告警（同 fingerprint、startsAt）→ `replayed`，无新事故、无新 Run | 4② |
| 08:29:16 | human_owned 下 `correct` 补充事实 → 200，只记输入与审计；Run、mode 不变 | 4③ |
| 08:29:37 / 08:29:41 | `register_remediation` → observing_recovery → `close`：会话 `authority_revoked`、waiting_human Run cancelled、lifecycle closed；页面只剩 reopen/takeover/follow_up/correct | 3 |
| 08:29:48 | closed 上 `new_run` → 409 `REOPEN_REQUIRED`，无写入 | 3 |
| 08:29:49 | `reopen` → open，无新 Run、无授权会话；页面出现 close、无 reopen；takeover 审计行保留 | 3、4 |

## 场景 B：事故 `a7eeac26`（故障 `f12-live-20261016-b`，08:34:12 注入 → 08:44:46 恢复）

| 时间（UTC） | 动作 → 结果 |
|---|---|
| 08:41:04 | 告警开事故与 Run `169ee055`（automatic） |
| 08:41:36 | 运行中 `close` → Run cancelled、lifecycle closed；08:41:44 该 Run 第 3 轮写回为 `late_result`，无发布 |
| 08:41:37 | closed 上 `follow_up` → 409 `REOPEN_REQUIRED` |
| 08:42:29 | `reopen`（无新 Run）→ `follow_up` 开 Run `6390989a`，08:44 发布结论 |

## 结果

`summary.json` 四步检查全部通过（step1–step4 共 30 项）。偏离合同运行计划之处，照实记录：

- 4② 的「同一告警再次 firing」是手工重投：Alertmanager `repeat_interval` 为 1 小时，窗口内没有真实重发。重投体用库中存下的同一条告警（fingerprint、startsAt 不变），走 `/intake/alertmanager` 与 Alertmanager 相同的 Bearer 认证，所以是模拟重投，不算真实重发。
- 控制动作通过 HTTP 提交与页面表单相同的字段（同一路由与 Origin 校验），没有用 Playwright 点按钮；页面状态以每步后保存的 HTML 为证。
- 观察到的现有行为（G5 沿用，不属本 PR 改动）：每次续开新 Run 都会撤销自动授权的观察会话，事故随之成为复盘生成候选，worker 在场景 A 中做了 6 次复盘生成尝试，前 5 次因水位变化 `WATERMARK_MOVED` 作废，关闭后第 6 次（08:31:58）生成版本 1（under_review）。这抬高了模型费用，已开 #255 跟踪。
- 用户 2026-10-16 裁决：上述两处偏离（4② 手工重投、控制经等价 HTTP 表单提交）按「实际通过」接受，F12 `passes` 据此翻转（#255 跟踪复盘重复生成，不影响本判定）。

## trace（project `opspilot-lab-f12-live-20261016`，longlived）

14 个根 run 的链接在 `summary.json` 的 `trace.roots`；主要几条：

| 根 run | 关联 | 链接 |
|---|---|---|
| `opspilot.run` | A 首个 Run `bb64bec6` | [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/1a70f852-5e85-46ad-9bb8-19125103d8ed/r/00000000-0000-0000-4fe6-01d82b01b652?poll=true) |
| `opspilot.run` | A 结论后追问 `9e760c03` | [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/1a70f852-5e85-46ad-9bb8-19125103d8ed/r/00000000-0000-0000-9c2c-6ecce7c00891?poll=true) |
| `opspilot.run` | A 纠正假设后 `c7d8b699`（resume 后一次） | [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/1a70f852-5e85-46ad-9bb8-19125103d8ed/r/00000000-0000-0000-cae2-c0c7b9f25f81?poll=true) |
| `opspilot.run` | A 被接管的 `1f53e856` | [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/1a70f852-5e85-46ad-9bb8-19125103d8ed/r/00000000-0000-0000-d845-7d402c7494ec?poll=true) |
| `opspilot.run` | B 被关闭的 `169ee055` | [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/1a70f852-5e85-46ad-9bb8-19125103d8ed/r/00000000-0000-0000-72bf-de6db600f7f3?poll=true) |
| `opspilot.run` | B 重新打开后追问 `6390989a` | [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/1a70f852-5e85-46ad-9bb8-19125103d8ed/r/00000000-0000-0000-3c2f-2596659ff683?poll=true) |

## 费用

DeepSeek 余额 203.44 → 200.49 CNY，共 2.95 CNY（赠送余额扣减，充值余额 200.02 不变），覆盖 7 个调查 Run 尝试与 6 次复盘生成。

## 收尾

两次故障均已恢复（flagd 全部 off），load-generator 回到 2 用户，web/worker/PG 已停，重负载锁已释放（08:05:09–08:47）。这是 kind 实验环境上的有界运行，不是生产证明。
