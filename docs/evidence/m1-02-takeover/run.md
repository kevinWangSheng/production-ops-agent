# M1-02 第 3b 步：接管（human_owned）真实运行（2026-10-07）

本机临时 PostgreSQL 17.9（端口 55497，数据目录在会话临时目录，未碰 55431 lab），新库 `make migrate` → `schema upgraded: 0005_incident_mode`；工作台 `python -m opspilot.web serve`（fixture profile，身份文件只列 `checkout-prod`），无 worker、无模型调用。步骤：提交事故 → 登记处置（会话 authorized）→ 接管 → 同键重放 → human_owned 下 resume 被拒 → human_owned 下再登记处置（新会话 authorized，无新 Run）→ 页面与库行。命令输出原样如下（鉴权与口令不入库）。

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
