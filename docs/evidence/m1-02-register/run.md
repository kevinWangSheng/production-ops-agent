# M1-02 第 3 步：登记处置真实运行（2026-10-07）

本机临时 PostgreSQL 17.9（端口 55491，数据目录在会话临时目录，未碰 55431 lab），空库 `make migrate` → `schema upgraded: 0004_target_identity`；工作台 `python -m opspilot.web serve`（fixture profile，`OPSPILOT_TARGET_IDENTITIES` 只列 `checkout-prod`），无 worker、无模型调用。步骤：提交事故 → 未登记目标被拒 → 登记处置 → 同键重放 → 过期表单 → 页面 → 暂停撤销。命令输出原样如下（鉴权与口令不入库）。

```
--- intake
{"incident_id":"dca0ef1c-2e1c-53ac-89e9-4dab55c603ab","run_id":"4052d45e-adc0-5faa-bb06-e085b3cc7df9","replayed":false,"sequence":1}
--- unknown target
{"code":"UNKNOWN_TARGET"}
--- register
{"incident_id":"dca0ef1c-2e1c-53ac-89e9-4dab55c603ab","action":"register_remediation","generation":1,"replayed":false,"sequence":2}
--- replay same key
{"incident_id":"dca0ef1c-2e1c-53ac-89e9-4dab55c603ab","action":"register_remediation","generation":1,"replayed":true,"sequence":2}
--- stale form
{"code":"CONTROL_CONFLICT","current_generation":1}
--- page (grep)
<span class="badge">authorized</span>
checkout:v2.0.3
id="observation-sessions"
lifecycle <span class="badge">observing_recovery</span>
otel-demo-checkout@cb52da44e49b
--- db rows after register, then after pause
     lifecycle      | state  | control_generation | observation_generation 
--------------------+--------+--------------------+------------------------
 observing_recovery | queued |                  1 |                      1
(1 row)

   state    | ctl_gen | obs_gen |     health_profile_revision     |    revision     | namespace | max_samples | interval_s | sustained_s | job_seq 
------------+---------+---------+---------------------------------+-----------------+-----------+-------------+------------+-------------+---------
 authorized |       1 |       1 | otel-demo-checkout@cb52da44e49b | checkout:v2.0.3 | checkout  |          40 |         60 |         600 |       1
(1 row)

        action        | expected_generation | resulting_generation | actor |    revision     
----------------------+---------------------+----------------------+-------+-----------------
 register_remediation |                   0 |                    1 | demo  | checkout:v2.0.3
(1 row)

 resource_uid  | integration_id |   cluster_uid   | namespace 
---------------+----------------+-----------------+-----------
 checkout-prod | fixture        | fixture-cluster | checkout
(1 row)

     lifecycle      | state  | control_generation 
--------------------+--------+--------------------
 observing_recovery | paused |                  2
(1 row)

  state  |   ended_reason    | active_sample_job_id 
---------+-------------------+----------------------
 revoked | authority_revoked | 
(1 row)

   ended_reason    | transition |  lifecycle_before  |  lifecycle_after   
-------------------+------------+--------------------+--------------------
 authority_revoked |            | observing_recovery | observing_recovery
(1 row)

```
