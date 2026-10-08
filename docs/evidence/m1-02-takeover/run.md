# M1-02 第 3b 步：接管（human_owned）真实运行（2026-10-07）

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

## 有界真实 Run：进行中接管（2026-10-07，AGENTS.md 真实 Run 证据门）

`scripts/m1_takeover_evidence.py`：fixture 工具 profile + 真实 DeepSeek（上限 3 次模型请求）+ `OPSPILOT_TRACE=lab`（LangSmith project `opspilot-lab-takeover-2026-10-07`，保留 `longlived`），临时 PG 55497。`InvestigationRunner` 在线程里跑 Run；主线程等到 Run 行 `running` 且持租约后执行 `takeover(expected_generation=0)`。冻结摘要：[live-runs/770e781b-d547-4953-a343-a8e61152efb3/summary.json](live-runs/770e781b-d547-4953-a343-a8e61152efb3/summary.json)（目录内仅此一文件；原始 ledger sha256 `713b5a87…731f9`，4824 字节，在 gitignore 的 `tmp/lab-ledgers/`，不入库）。

- 时序：第 1 次模型请求 `started_at 2026-10-08T05:04:13.414Z`；接管事务 `at 05:04:13.479Z`（审计行 `created_at 05:04:13.482Z`，generation 0 → 1）。该请求在接管前已发出、接管后返回，其提交被代际栅栏拒绝：首次尝试 `status=control_denied`、`steps_committed=0`、无报告、无证据。
- 接管后：Run `waiting_human`，owner/租约为空；事故 `mode=human_owned`、`control_generation=1`；`claimable_incidents` 不含该事故；第二次 `runner.resume` → `handed_off / AWAITING_HUMAN`，模型请求总数仍为 1（`no_model_request_after_takeover_returned=true`）。
- trace：`otel_trace_id a0e64a03ca960ee12078c4e27d36a2f0`，LangSmith run `00000000-0000-0000-212a-04c52f48bd3d`，回读 `found`，链接在 summary.json `trace.url`。
- 费用：1 次请求 1194/72 tokens，上界 0.003246 CNY；DeepSeek 余额 5.82 → 5.82 CNY（低于显示精度）。
