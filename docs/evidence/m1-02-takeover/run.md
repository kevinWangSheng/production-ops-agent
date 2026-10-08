# M1-02 第 3b 步：接管（human_owned）真实运行（2026-10-07）

> **修复前记录**（PR #123 独立审查前的行为）：下面第一段演示里「human_owned 下 resume → `ILLEGAL_TRANSITION`」已不再成立——审查第 1 条后 resume 只解除主体暂停；再次 takeover 也不再 `ILLEGAL_TRANSITION`。现行行为见本文末尾「修复后工作台演示」。

本机临时 PostgreSQL 17.9（端口 55497，数据目录在会话临时目录，未碰 55431 lab），新库 `make migrate` → `schema upgraded: 0005_incident_mode`；工作台 `python -m opspilot.web serve`（fixture profile，身份文件只列 `checkout-prod`），无 worker、无模型调用。步骤：提交事故 → 登记处置（会话 authorized）→ 接管 → 同键重放 → human_owned 下 resume 被拒 → human_owned 下再登记处置（新会话 authorized，无新 Run）→ 页面与库行。命令输出摘录如下（含 `...` 的行是省略了不变字段的摘录，不是原样；鉴权与口令不入库）。

```
--- intake
{"incident_id":"06337259-c9a6-567d-99b3-6c0dfcf8f6af","run_id":"91139667-03f8-52dc-a96b-4f48227d92e8","replayed":false,"sequence":1}
--- register (expected_generation 0)
{"...":"register_remediation","generation":1,"replayed":false}
--- takeover (expected_generation 1)
{"...":"takeover","generation":2,"replayed":false}
--- takeover replay (same idempotency key)
{"...":"takeover","generation":2,"replayed":true}
--- resume under human_owned (expected_generation 2)
{"code":"ILLEGAL_TRANSITION"}
--- register under human_owned (expected_generation 2, revision checkout:v2.0.4)
{"...":"register_remediation","generation":3,"replayed":false}
--- page (grep)
   1 <span class="badge">authorized</span>
   1 <span class="badge">revoked</span> <span class="muted">authority_revoked</span>
   1 id="incident-mode">human_owned
   1 id="run-state">waiting_human
   1 lifecycle <span class="badge">observing_recovery</span>
--- db rows
     lifecycle      | state  |    mode     | control_generation | observation_generation 
--------------------+--------+-------------+--------------------+------------------------
 observing_recovery | queued | human_owned |                  3 |                      2
(1 row)

                run_id                |     state     | owner | lease_until 
--------------------------------------+---------------+-------+-------------
 91139667-03f8-52dc-a96b-4f48227d92e8 | waiting_human |       | 
(1 row)

   state    |   ended_reason    | ctl_gen |    revision     
------------+-------------------+---------+-----------------
 revoked    | authority_revoked |       1 | checkout:v2.0.3
 authorized |                   |       3 | checkout:v2.0.4
(2 rows)

        action        | expected_generation | resulting_generation | actor 
----------------------+---------------------+----------------------+-------
 register_remediation |                   0 |                    1 | demo
 takeover             |                   1 |                    2 | demo
 register_remediation |                   2 |                    3 | demo
(3 rows)

```

## 有界真实 Run（第一次，已被下一节替代）：进行中接管（2026-10-07）

> **被替代的原因**（机器人审查 PR #123）：这次运行在「Run 行 running 且持租约」后就接管，没有证明模型 HTTP 请求已经发出（`model_requests_started_before=0` 正是这个口径的后果）。保留以供对照；现行证据见下一节。

`scripts/m1_takeover_evidence.py`：fixture 工具 profile + 真实 DeepSeek（上限 3 次模型请求）+ `OPSPILOT_TRACE=lab`（LangSmith project `opspilot-lab-takeover-2026-10-07`，保留 `longlived`），临时 PG 55497。`InvestigationRunner` 在线程里跑 Run；主线程等到 Run 行 `running` 且持租约后执行 `takeover(expected_generation=0)`。冻结摘要：[live-runs/770e781b-d547-4953-a343-a8e61152efb3/summary.json](live-runs/770e781b-d547-4953-a343-a8e61152efb3/summary.json)（目录内仅此一文件；原始 ledger sha256 `713b5a87…731f9`，4824 字节，在 gitignore 的 `tmp/lab-ledgers/`，不入库）。

- 时序：第 1 次模型请求 `started_at 2026-10-08T05:04:13.414Z`（`request_started_at`，每次 HTTP 请求发出前的墙钟）；`takeover.at 05:04:13.479Z` 是**调用 `control()` 之前**取的数据库时钟，不是提交时刻——提交时刻以审计行 `created_at 05:04:13.482Z`（generation 0 → 1）为准。`model_requests_started_before` = 调用 takeover 前已发出的模型请求数（本次为 0：请求在 takeover 调用前 65 ms 发出，但 `RecordingClient` 在请求返回后才登记，所以计数为 0；以 `request_started_at` 对照 `takeover.at` 判断先后）。`no_model_request_after_takeover_returned` = 第二次 `runner.resume` 前后模型请求总数相等（1 == 1），即接管返回后没有新的模型请求。字段名已冻结在 summary.json，这里只做口径说明。该请求在接管前已发出、接管后返回，其提交被代际栅栏拒绝：首次尝试 `status=control_denied`、`steps_committed=0`、无报告、无证据。
- 接管后：Run `waiting_human`，owner/租约为空；事故 `mode=human_owned`、`control_generation=1`；`claimable_incidents` 不含该事故；第二次 `runner.resume` → `handed_off / AWAITING_HUMAN`，模型请求总数仍为 1（`no_model_request_after_takeover_returned=true`）。
- trace：`otel_trace_id a0e64a03ca960ee12078c4e27d36a2f0`，LangSmith run `00000000-0000-0000-212a-04c52f48bd3d`，回读 `found`，链接在 summary.json `trace.url`。
- 费用：1 次请求 1194/72 tokens，上界 0.003246 CNY；DeepSeek 余额 5.82 → 5.82 CNY（低于显示精度）。

## 有界真实 Run（现行）：模型请求在途时接管（2026-10-07）

`scripts/m1_takeover_evidence.py`（修订：主线程先等 Run 行 `running` 且持租约，再等 `TimestampedRecordingClient` 在发出第一次 HTTP 请求前置的 `threading.Event`，然后才调用 takeover），fixture 工具 profile + 真实 DeepSeek（上限 3 次请求）+ `OPSPILOT_TRACE=lab`（project `opspilot-lab-takeover-2026-10-07`，`longlived`），临时 PG 55497。冻结摘要：[live-runs/7f1d7572-da37-4967-afcc-21a566acafb8/summary.json](live-runs/7f1d7572-da37-4967-afcc-21a566acafb8/summary.json)（目录内仅此一文件；原始 ledger sha256 `215bb589…`，4999 字节，在 gitignore 的 `tmp/lab-ledgers/`，不入库）。

- 口径：`request_started_at` 是每次 HTTP 请求发出前的墙钟；`takeover.at` 是调用 `control()` 之前取的数据库时钟，提交时刻以审计行 `created_at` 为准；`model_requests_started_before` 现按「已发出的请求数」计（事件置位时登记），`model_requests_returned_before` 是调用 takeover 时已返回的请求数；`no_model_request_after_takeover_returned` = 第二次 `runner.resume` 前后请求总数相等。
- 时序：第 1 次模型请求 `started_at 2026-10-08T06:18:44.838Z`；`takeover.at 06:18:44.912Z`，审计行 `created_at 06:18:44.915Z`（generation 0 → 1）；`model_requests_started_before=1`、`model_requests_returned_before=0`——接管时请求确实在途。该请求返回后提交被代际栅栏拒绝：首次尝试 `control_denied`、`steps_committed=0`、无报告、无证据。
- 接管后：Run `waiting_human`，owner/租约为空；事故 `human_owned`、`control_generation=1`；不在可领取列表；第二次 `runner.resume` → `handed_off / AWAITING_HUMAN`，模型请求总数仍为 1（`no_model_request_after_takeover_returned=true`）。
- trace：`otel_trace_id 1c3d4fa43732081c83dea4da45ccb4d1`，LangSmith run `00000000-0000-0000-7160-952400db8428`，回读 `found`，链接在 summary.json `trace.url`。
- 费用：1 次请求 1197/80 tokens，上界 0.003322 CNY；DeepSeek 余额 5.81 → 5.81 CNY（低于显示精度；两次真实 Run 合计余额 5.82 → 5.81）。

## 修复后工作台演示（2026-10-07，审查第 1、2 条与机器人「再次接管」之后）

新库（55497 `opspilot_live4`）`migrate` → 0005；工作台同上配置。步骤与结果（命令输出摘录，`...` 为省略的不变字段）：

```
--- intake                                   201, incident e95f42b4-…
--- pause (expected 0)                       generation 1
--- takeover (expected 1)                    generation 2
--- resume under human_owned (expected 2)    generation 3   ← 解除主体暂停
    rows after resume: incident state running / mode human_owned / gen 3; run waiting_human owner null
--- register_remediation (expected 3)        generation 4   ← human_owned 下单独授权观察
--- (工程 SQL) 把旧 Run deadline 置为过去
--- follow_up with text (expected 4)         generation 5   ← 只记录，不续开
--- takeover again (expected 5)              generation 6   ← 撤销 human_owned 下登记的新观察
--- page (grep)
lifecycle <span class="badge">observing_recovery</span>
id="incident-mode">human_owned
id="run-state">waiting_human
<span class="badge">revoked</span> <span class="muted">authority_revoked</span>
--- db rows
     lifecycle      |  state  |    mode     | control_generation | observation_generation 
--------------------+---------+-------------+--------------------+------------------------
 observing_recovery | running | human_owned |                  6 |                      1
(1 row)

                run_id                |     state     | owner | lease_until 
--------------------------------------+---------------+-------+-------------
 5ed8bf2f-74d5-5b0e-a75a-21cdccd3d31d | waiting_human |       | 
(1 row)

  state  |   ended_reason    | ctl_gen 
---------+-------------------+---------
 revoked | authority_revoked |       4
(1 row)

   kind    |              text               | control_generation 
-----------+---------------------------------+--------------------
 follow_up | handled by the on-call engineer |                  5
(1 row)

        action        | expected_generation | resulting_generation 
----------------------+---------------------+----------------------
 pause                |                   0 |                    1
 takeover             |                   1 |                    2
 resume               |                   2 |                    3
 register_remediation |                   3 |                    4
 follow_up            |                   4 |                    5
 takeover             |                   5 |                    6
(6 rows)

```

结论：resume 后镜像 `running`、Run 仍 `waiting_human`；过期 Run 下的追问落为一行 `opspilot_inputs`（gen 5）、Run 集合不变；第二次 takeover 把 gen 4 授权的会话置为 `revoked/authority_revoked`，mode 与 Run 不变，六条审计行依次为 pause/takeover/resume/register_remediation/follow_up/takeover。
