> 裁决（2026-10-22）：用户裁决 D2-a（发布 checkout，R9 澄清为「与故障原因无关的部署」）、D5-a（否定部署须引用本 Run ok 证据并满足 (i)/(ii)/(iii) 之一，KSM 只作诊断）、D3-a（只列仓库证据已见的 KSM 序列，PR-3a 不碰 lab）；D1-a、D4-a 与其余可逆默认由 lead 按用户常设决定「预审待决项按推荐」采纳。任务记录见 [2026-10-17-f3.md](2026-10-17-f3.md)。

# F3 PR-3a 合同预审：竞争原因场景、工具描述 bump、rubric 与期望答案冻结

- 预审者：未参与实现的 Agent，只读；2026-10-22
- 基线：`main` @ `8f1f49c`（与 `origin/main` 一致；本地工作区仅有 `docs/tasks/2026-09-27-m1-01-report-contract.md` 未提交改动与 `.playwright-mcp/`，与本项无关）
- 依据：F3 任务记录与合同 r1（U1-b、U2-c、U3-a、U4-a、R9、第四节 rubric、第八节未确认项）；C3 §5（`docs/design/technical-proposal-2026-09-07.md:191-199`）；PRODUCT-CONSTRAINTS.md:16；评测套件合同 r8（`docs/tasks/2026-10-12-eval-suite.md:22-27`）；`evals/README.md`；PR #298/#299/#303 diff；PR #306 分支上的 F11 PR-4 合同（`/Users/shenghuikevin/dev/AI/poa-docs-1021/docs/tasks/2026-10-21-f11-pr4-contract.md`）；F2 PR-C 真实运行记录（`poa-f2-c` 分支 `docs/evidence/f2-alert-correlation-live/run.md`）
- 未运行任何测试、未碰 kind、未调用模型、未连 Prometheus。下文「已核对」指读代码、配置或已入库证据。

## 0. 结论（先读这段）

1. 前置状态：PR-1（#265）已于 2026-10-21 合并。PR-2 `feature/F3-coverage-gaps` 未开工：本地与远端都没有该分支，也没有 worktree、PR。PR-2 不改模型可见面（U5-a「不改提示词」），所以 PR-3a 与它可以并行，无文件重叠。
2. 有一个 R9/U1-b 写法与代码现状冲突，必须由用户裁决（D1）：告警 Run 的问题由 `question_for` 确定性生成，只含告警名、严重度、目标、startsAt 与 PromQL，从不含 annotation（`opspilot/alertmanager.py:230-247`，docstring 第 233-236 行）。所以「在提问中写明部署」对告警触发的 Run 无法实现，除非改输入合同。推荐的做法不改产品代码，也不改输入版本：告警开出事故后、worker 领取前，操作员发一条 `follow_up`（追问）写明部署。这条文字作为 `investigation_inputs` 在第 1 轮送达模型（`controls.py:580-608`、`context.py:1116-1170`、`loop.py:654-662`）。
3. 第二处需要裁决（D2）：harness 的发布动作写死 checkout（`scripts/f11_u2_harness.py:1166-1260`，patch 目标 `deployment checkout`，校验 `CHECKOUT_CONTAINER_MISSING`）。checkout 正是告警服务，也是 payment 故障的受害方。R9 写的是「与故障无关的服务」。推荐直接用 checkout，并把含义澄清为「与故障原因无关的部署」，理由见 D2。这样 PR-3a 不改 `f11_u2_harness.py`，与 #298 零重叠。
4. 工具描述在 `opspilot/tools/otel_demo.py:266-316`（`TOOL_SCHEMAS` 的模型可见描述；指标清单在 310-316 行），`tool_schema_revision` 是内容哈希（`otel_demo.py:651-669`）。当前值 `otel-demo-4d7168180749`，本地 `.venv` 实算确认。改一个字就自动 bump，没有测试钉住这个值（`git grep 4d7168180749 -- tests opspilot scripts` 无结果）。
5. 与 F11 PR-4（v6）不冲突：PR-3a 不改 `context.py`、输入版本（仍为 v5，`context.py:53-115`）、`prompt_revision`。PR-4 改的是 `prompt_revision`，不碰 `otel_demo.py`（PR-4 合同第 5 节允许改动文件清单）。两者都只需早于 PR-3b。「F11 PR-4 必须早于 F3 PR-3b」这一约束在仓库与 #306 合同里都没找到出处（未确认，按 lead 口述采纳）。
6. 冲突与排期：PR-3a 预计改动文件与 #298/#299/#303 都不重叠（第 4 节）。bump 合入 `main` 后，只有满足两个条件的 Run 会被 `blocked(INCOMPATIBLE_STATE)`：一是 Run 用旧代码建立或运行，二是之后被跑新代码的 worker 重新领取。F2 PR-C 的真实运行已于 10-22 08:33Z 完成并入库；#298、#303 不产生调查 Run。建议 PR-3a 随时可开工，合并时机只需避开别人「真实观察窗内 merge main 并重启 worker」的时刻。

## 1. 核实事实（逐项出处）

### 1.1 工具描述与 revision

- 模型可见描述：`otel_demo.py:266-316`（`TOOL_SCHEMAS[0]`，`METRICS_TOOL = "metrics_range_query"`，第 148 行）。指标清单第 310-316 行只列 OTel 指标：`traces_span_metrics_calls_total{service_name,span_name,status_code}`、`rpc_client_duration_milliseconds_count{...}`、`rpc_server_duration_milliseconds_bucket`、`app_payment_transactions_total`、`http_server_request_duration_seconds_count`。
- `expr` 参数描述写着「label values come from the service names listed for traces_search」（`otel_demo.py:322-327`）。kube-state-metrics 用 `namespace`/`deployment`/`replicaset`/`pod` 标签，不用 `service_name`，描述需一并说明，否则模型会按 `service_name` 过滤 KSM 序列而得到 `no_data`。
- 注册表侧描述（`ToolDescription`，`otel_demo.py:452-480` 起）没有指标清单，不需要改。两面都计入哈希。
- revision 计算：`_tool_schema_revision()` 对 `TOOL_SCHEMAS`、`ToolRegistry(_registrations()).revision`、`TRACE_PROJECTION`、`PROJECTION_REVISION` 求 `canonical_hash`，取前 12 位（`otel_demo.py:651-669`）。经 `otel_demo_versions()` 进入 Run 的 `versions`（`otel_demo.py:795-796`；`tools/profiles.py:40,73`）。
- 对在途 Run 的影响：C3 §5「bump 后被重新领取的在途 Run 进入 `blocked(INCOMPATIBLE_STATE)`……不得为避免 blocked 而不 bump」（`technical-proposal-2026-09-07.md:191-193,199`）。worker 领取时带自己的 `versions`（`opspilot/investigation/runner.py:207`）。旧代码 worker 继续运行不受影响，只有「新代码 worker 领取旧 Run」才 blocked。
- 不会被 PR-3a 牵动的版本：`PROJECTION_REVISION`（视图字节不变）、`prompt_revision`、`context_policy_revision`、输入版本。
- 已有测试不钉当前值：`tests/test_m1_series_note_contract.py:98-103` 只断言不等于某个旧组合；`tests/acceptance/test_f7_step3_source_secrets.py:80` 等是旧值常量。

### 1.2 kube-state-metrics 序列（只读仓库证据，未连 lab）

- lab 配置：`scripts/kind_lab/kube-state-metrics-values.yaml:1-19`，收集器 `deployments, pods, replicasets, services`，`prometheusScrape: true`，由 demo Prometheus 的 `kubernetes-service-endpoints` job 抓取。chart 版本见 `scripts/kind_lab.py:70,920-926`。
- 抓取间隔约 60 s：`opspilot/observer/profiles/otel-demo-checkout.json:109`「between two scrapes (60 s in the lab)」。
- 在 lab 上实际见过的序列：

| 序列 | 已见标签 | 出处 |
|---|---|---|
| `kube_deployment_status_replicas_available` | `namespace`, `deployment` | 健康 profile 在用（`otel-demo-checkout.json:28-30,110-126`）；大量 m1-02 证据 |
| `kube_deployment_status_replicas_ready` | 同上 | `docs/evidence/m1-02-live-2/reprojection-0336c04/*` |
| `kube_pod_container_info` | `namespace`, `pod`, `uid`, `image_id`（`…@sha256:`） | `docs/evidence/f11-pr2-telemetry-probe/run.md:8`（KSM 2.20.0）；`release_evaluation.py:203` |
| `kube_pod_owner` | `uid`, `owner_kind="ReplicaSet"`, `owner_name="checkout-<hash>"` | 同上 `run.md:9`；`release_evaluation.py:191-195` |
| `kube_replicaset_owner` | `namespace`, `replicaset`, `owner_kind="Deployment"`, `owner_name` | 同上 `run.md:10`；`release_evaluation.py:183-188` |
| `kube_pod_status_phase`、`kube_pod_container_status_restarts_total` | 未逐一核对 | `docs/evidence/m1-02-lab/run.md` 等 |

- 已知为空：`kube_replicaset_labels` 0 条（`f11-pr2-telemetry-probe/run.md:11`）。
- 未确认（仓库中只在合同第八节作为猜测出现）：`kube_replicaset_created`、`kube_deployment_metadata_generation`、`kube_deployment_status_observed_generation`、`kube_deployment_spec_replicas`、`kube_pod_start_time`。这些能直接给出部署时间，但从未在 lab 上见过。
- F11 探测是用「调查侧只读账号」查的（`f11-pr2-telemetry-probe/run.md:3`），说明 KSM 序列经调查实际使用的只读路径可达。PromQL 守卫只拒 `@`、`offset` 与超窗选择器，不按标签或指标名过滤（`otel_demo.py:869-890`）；目标隔离靠实例（`otel_demo.py:11-14`）。所以加描述不需要新权限。
- 需要注意：仅改配置的发布不改 image digest（`f11-pr2-telemetry-probe/run.md` 结论段）。部署在 KSM 上表现为新 ReplicaSet 名（`kube_replicaset_owner` 新序列出现）、`kube_pod_owner` 换 owner，以及可能的 available 副本数短暂变化；`kube_pod_container_info` 的 image 不变。描述里应写明这一点，否则模型可能只看 image，得出「没有部署」。

### 1.3 harness 的发布与 flag 接口（`scripts/f11_u2_harness.py`）

- 发布：`publish(ledger, release_id, *, dry_run=False)`，第 1166-1260 行。strategic patch 只给 checkout 容器加环境变量 `OPSPILOT_U2_RELEASE_ID=<release_id>`（1169-1182），带 `resourceVersion` CAS（1213-1216）。若 `fault-active.json` 存在拒绝（1209-1210）。R = patch 成功回执时间，不是 Pod Ready（1236）。写 `release.json` 与 `timeline.jsonl`。dry-run 只打印 kubectl 命令（1198-1200）。
- 快照：`snapshot()` 写死 `deployment checkout`（1046-1106）。
- 故障：`scheduled_fault(ledger, offset, duration, flag, variant, normal, url, method, propagation_seconds=180, dry_run)`，第 1979-2113 行。计划时刻 = `release.json.released_at + offset`（2010-2011）。约束 `offset+duration ≤ 7200`（1992-2002）。注入后用 flagd `Resolve<method>` 确认 variant 生效（`await_flag` 1895-1947），维持 `duration` 秒后在 `finally` 自动恢复（`restore_fault` 1950-1976）。全局互斥 `lab_ledgers()/fault-active.json`。
- flag：`kind_lab.FLAG = "paymentFailure"`，`FAULT_VARIANT = "100%"`，`NORMAL_VARIANT = "off"`（`scripts/kind_lab.py:102-110`）。文档示例 `--method Int`（`docs/development.md` harness 节）。method 实际类型未在本次核对，harness 运行时会校验。
- 占用：`occupation claim/show/release` 与 `reserved(holder, batch)`（265-327），跨 worktree 共享。
- 副作用：走 CLI 的 `release` 会往共享 F11 U2 批次日志 `tmp/lab-ledgers/run-starts.jsonl` 追加哈希链记录（`start_round`，192-234；`main` 2412-2420）。若 F3 直接 import `publish`/`scheduled_fault`，就不会写入 F11 的批次日志，同时仍共用占用与 `fault-active.json` 互斥。

### 1.4 告警与提问通道

- 唯一告警规则：`CheckoutPlaceOrderErrorRatioHigh`，checkout PlaceOrder 错误比（rate 5m）> 0.5，`for: 2m`（`scripts/kind_lab/values.yaml:182-199`）。路由到工作台 `/intake/alertmanager`，`group_wait 10s`、`repeat_interval 1h`（`alertmanager-values.yaml`）。paymentFailure 让每笔订单失败（`values.yaml` 注释）。
- 告警 Run 的问题：`question_for`（`alertmanager.py:230-247`）。由 `intake_alert` 写入（`web/service.py:401,496-512`）。annotation 只作为 v4 的 `alert_context` 不可信消息送达（`context.py:66-74,650-651`），内容来自 lab 规则的固定文本，无法携带某次部署。
- 追问通道：`POST /incidents/{id}/control`，`action=follow_up`，`text`，`expected_generation`。需要 UI Basic Auth 与 Origin（`web/app.py:592-623`）。文字限 `INPUT_CONTENT_FIELD_MAX_CHARS=8192`，并拒绝控制字符（`web/service.py:_MAX_TEXT`；`opspilot/intake.py:183-199`）。
  - 非人工接管时，追问把 Run 置为 `queued`、代际 +1，并插入 `opspilot_inputs` 行（`persistence/controls.py:580-608`）。
  - 模型侧以 `investigation_inputs` 尾随消息出现在每轮（`context.py:1099,1116-1170`；`loop.py:650-662`），字段白名单为 `text/channel/question`，并经凭据脱敏。
  - 不改输入版本（`version` 只看 scope facts，`context.py:516-527`）。
- 已有真实先例：F2 PR-C live 证明追问文字进入了模型输入，只是路径不同（`poa-f2-c:docs/evidence/f2-alert-correlation-live/run.md:10`）。同一记录还发现：对「最终报告被拒而交接」的 Run 追问会被 `INCOMPATIBLE_STATE` 挡下（同文件 72-96 行，失败点 `context.py:960,1229`）。F3 推荐的时机是「首次领取之前」，此时 Run 还没有任何 step，不经过该重建路径。这一点是按代码推断的，未实测。

### 1.5 评测侧答案隔离的既有做法

- 评测套件：`evals/scenarios/<id>/scenario.json` 只放被测可见的刺激，经白名单键校验（`opspilot/evaluation/model.py:228-240`）。`answer.json` 只给评测侧（`evals/README.md`；`model.py:1-6`「never an input of investigator_input」）。测试有 canary 先例（`tests/acceptance/eval_c_support.py:32` `PRIVATE_ANSWER`）。
- 不能把 F3 场景放进 `evals/scenarios/`：`load_scenarios` 会遍历该目录全部子目录（`model.py:406-408`）。多个测试与 P7 驱动依赖 D1 的 6 个场景集合（`tests/unit/test_eval_b1_impl.py:878-890`、`tests/acceptance/test_eval_p7_driver.py:1160`、`scripts/eval_live.py:1756,1870,1889`）。评测合同还规定「新增场景另开版本」（eval-suite.md:22）。另外，F3 的答案需要部署字段，现有 `_ANSWER_KEYS`/`_INJECTED_KEYS` 是封闭键集（`model.py:232-240`），装不下。

## 2. 九类必答（PR-3a 范围，兼顾 PR-3b 的前提）

1. **时间**
   - 已定：
     - R = harness patch 回执时间，宿主机 UTC（`f11_u2_harness.py:1236`）。
     - 故障计划时刻 = R + offset（`:2010-2011`）。生效时刻 = flagd 首次返回目标 variant（`await_flag`）。
     - 告警 Run 的默认查询窗从告警 startsAt 前 1 小时到框架末端（`otel_demo.py:458-468` 描述；M1-04 J4），所以 R 只要落在 startsAt 前 1 小时内就可见。
     - KSM 抓取粒度约 60 s。
   - 待决（可逆默认 R-1、R-2）：offset 与 duration 取值；追问里呈现哪个时刻（R，还是 rollout 完成）。
   - 「≥5 分钟」的口径：推荐按「rollout 完成 → flagd 生效」计，而不只是 R → 生效。
2. **计数与口径**
   - 已定：每次执行一个部署、一个故障、一个告警事故、一个 Run；第 5 步 2 次（U4-a）。
   - 待决：「窗口内只有这一次 checkout 部署」作为前提（R-6）。
     - 原因：F11 U2 校准与 F2 都在 lab 上发布或注入。另一次 checkout 发布若落在 startsAt−1h 内，会成为第二个竞争原因，答案不再唯一。
     - 两次 F3 执行之间同理：第一次的部署会落在第二次的窗口里，除非间隔 ≥70 分钟，或答案显式列出。
3. **状态转移**
   - 已定：
     - 告警投递 → 事故 + queued Run（`service.py:496-512`）。
     - 追问 → 同一 Run 重新 queued、代际 +1、写入一行输入（`controls.py:580-608`）。
     - 故障在 duration 后由 `finally` 自动恢复。
     - rubric 与答案在 PR-3a 合并时冻结，之后只能收紧（合同第四节）。
   - 待决：
     - 追问与 worker 领取的先后（D1：推荐先追问、后启动 worker）。
     - F2 #299 合并后，同 fingerprint 告警遇到未 closed 的事故会被附加，不开新 Run（F2 PR-C 合同 C3，`poa-f2-c:docs/tasks/2026-10-21-f2-c-contract.md:88`）。所以每次 F3 执行前，该目标上同告警的旧事故必须 closed（R-6 前提）。
4. **主体与权限**
   - 已定：
     - 发布与 flag 变更是测试 harness 的 lab 管理权限，不是产品权限（PRODUCT-CONSTRAINTS.md:16）。
     - 追问是 lab UI Basic Auth 人工主体，经产品控制入口发出，记入审计（`web/app.py:594`）。
     - KSM 读取沿用调查侧只读 Prometheus 账号，不加凭据或连接器（U1-b）。
     - adapter 仍只发 GET（`otel_demo.py:1013`）。
   - 待决：无。实现者注意：驱动脚本不得把 UI 凭据写入 ledger、日志或证据，凭据从私有 env 读取。
5. **数据合同**
   - 已定：
     - `tool_schema_revision` 由内容哈希自动 bump（C3 §5）。
     - 不迁移，不改输入版本（v5），不改 `prompt_revision` 或 `PROJECTION_REVISION`，不改报告 schema（R5）。
     - 部署顺序：仅代码。web 与 worker 必须同版本，否则 claim 时 blocked（`docs/development.md:85-107` 演示段说明）。
   - 待决：rubric 与答案的位置与格式（R-3、R-4）。
6. **旧数据与已归档证据兼容**
   - 已定：
     - 旧 revision 下的在途 Run 被新 worker 领取时 blocked（C3 §5:192）。
     - 已归档证据不重签。
     - 评测录制带自己的 versions，回放时按维度比对（`opspilot/evaluation/replay.py:343`）。所以 bump 后，旧录制与新代码在 `tool_schema_revision` 维度上不可比；PR-1 已经 bump 过一次。
     - 评测基线尚未记录（P7 在 M3），代价小（合同 U1-b 栏）。
   - 未确认：#300（P7 r3 judge）是否依赖当前 `tool_schema_revision` 的录制做回放。
7. **并发与水位**
   - 已定：lab 单一共享。用 `occupation`/`reserved` 与 `fault-active.json` 互斥（`f11_u2_harness.py:265-327,1209-1210,2015-2018`）。
   - PR-3a 自身不占 lab。
   - PR-3b 前提见 R-6：无其他 checkout 发布、无 F11 发布观察在途、无同告警开放事故、`ALERTS` 为空、flagd 全 off、swap 检查。
8. **外部调用**
   - PR-3a：不调模型，不跑 Run，不碰 kind。dry-run 不发任何 kubectl、flagd、HTTP 请求。
   - 唯一可能的外呼是 D3 的只读 Prometheus 序列探测，需要裁决。
9. **验收投影与 `passes`**
   - PR-3a 不动 `passes`。交付物：冻结的 rubric 与评测侧答案、场景驱动与 dry-run、描述 bump。
   - 第 5 步判定在 PR-3b。新场景 id 用 `F3:` 前缀挂到 `PRODUCT_CONSTRAINT_SCENARIO_INDEX`（R11）。

## 3. 决定表

### 需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| D1 | 告警触发的 Run 怎样「在提问中呈现该部署」（U1-b 与 R9 的落地通道） | **a**：告警开出事故后、worker 启动前，操作员经 `/incidents/{id}/control` 发 `follow_up`，文本写明部署；随后启动 worker，第 1 轮即可见。<br>　优点：不改产品代码、输入版本、`prompt_revision`。<br>　代价：PR-3b 要编排「web 先起、worker 后起」；追问记为人工控制，第 6 步审计里会出现一条人工 follow_up。<br>**a′**：同 a，但 worker 常驻，告警后尽快追问。<br>　代价：与领取竞争。若 Run 先结束，追问会走续开或交接路径（F2 已见交接后追问被 `INCOMPATIBLE_STATE` 挡下）。不确定性高。<br>**b**：UI 人工建事故，问题文本写告警与部署；告警照常触发。<br>　代价：会多一个告警事故和一个 Run（费用翻倍、两份报告）；若静默告警，则不满足 R9「告警由现有规则触发」。<br>**c**：改 `question_for` 或加 scope fact。<br>　代价：改 I6 输入合同、要新的输入版本，与 F11 PR-4 的 v6 抢版本号，违反「PR-3a 不碰输入」。<br>**d**：改 lab 告警 annotation（模板里查 KSM）。<br>　代价：改共享告警规则，影响 F2、F11 的 lab 运行；内容不可控 | **a** | 场景驱动、PR-3b 运行手册、第 6 步审计口径 | |
| D2 | 部署哪个服务（R9「与故障无关的服务」 vs harness 只能发布 checkout） | **a**：发布 checkout（告警服务本身），只改环境变量。<br>　优点：harness 发布动作原样复用，不改 `f11_u2_harness.py`，与 #298 零重叠；竞争原因最强（部署落在出问题的服务上）。<br>　代价：R9 文字须澄清为「与故障原因无关的部署」。rubric 第 5 条括号里的例子「该服务的错误率在部署前后无变化」要改写为「部署后到故障生效前 checkout 错误率无变化，且错误位于 payment Charge」，这是对 rubric 例子的澄清，不放宽判据。<br>**b**：把 `publish`/`snapshot` 参数化，改发布不在 checkout 调用链上的服务（如 `ad`、`recommendation`）。<br>　代价：改 harness 两个函数，与 #298 同文件，需协调合并；部署离故障路径太远，模型不必取证就能排除，竞争性弱；`deployment` 与容器名要逐个核对（未确认）。<br>**c**：发布 payment 的兄弟服务（如 `currency`、`shipping`）。<br>　代价：同 b，且这些服务在 checkout 调用链上，答案更难写定 | **a** | R9 文字、rubric 第 5 条措辞、答案文件、harness 是否改动 | |
| D3 | 描述里列哪些 KSM 序列（第八节未确认项） | **a**：只列仓库证据已见的序列与标签（1.2 表；`kube_replicaset_labels` 不列），并写明「仅配置变更时 image digest 不变，部署体现为新 ReplicaSet 与 Pod owner 变化」。<br>　PR-3a 不碰 lab。<br>　代价：模型拿不到直接的创建时间，只能从新 ReplicaSet 序列的出现时刻推断（误差约 1 个抓取间隔）。<br>**b**：先经只读代理做一次序列名探测（`/api/v1/series` 或 `label/__name__/values`，只读、秒级，不执行 kind 命令，但要求 lab 在线并协调占用），确认后再加 `kube_replicaset_created`、`kube_deployment_metadata_generation` 等，探测结果入证据。<br>　代价：在 PR-3a 引入一次 lab 只读访问。<br>**c**：先按 a 合并，PR-3b 预检时再探测，必要时补一次 bump。<br>　代价：多一次 bump 和一个 PR，且必须在 PR-3b 首个 Run 之前完成 | **b**；lab 不可用时退回 **a**，不选 c | 模型可见面、第 5 步证据质量、lab 排期 | |
| D4 | 追问文字写什么（这是验收刺激，被测模型可见） | **a**：只陈述事实：`Operator note: Deployment otel-demo/checkout was rolled out at <R, UTC 秒精度> (release <中性 id>).`<br>　不评价变更内容，不提示「是否相关」。<br>**b**：同 a，加「configuration-only change」。<br>　代价：提前把答案的一半告诉模型，竞争检验变弱。<br>**c**：同 a，加「Check whether it caused the alert」。<br>　代价：诱导性提问，但更接近真实值班语境 | **a**。release id 不得含 `f3`、`unrelated`、`decoy`、`payment` 等可泄露语义的词，在 harness 环境变量里同样中性 | 验收刺激、答案隔离 | |
| D5 | rubric 第 5 条「否定确实针对那次部署」怎样落到评测侧期望答案（冻结前定，之后只能收紧） | **a**：否定须引用本 Run 的 ok 证据，并至少满足一条：(i) 部署（R 到 rollout 完成）之后、故障生效之前，checkout PlaceOrder 错误率或错误 span 无升高；(ii) 错误 span 位于 payment `Charge`（沿用 `payment-charge-error-span-v1` 证据特征）；(iii) 错误起点晚于 rollout 完成 ≥5 分钟，且有 KSM 或 span 证据佐证时间。<br>　只靠追问文字里的时刻、不引证据，不算。<br>　不要求模型用 KSM 证实部署本身，只记录作 U1-b 效果诊断。<br>**b**：同 a，另要求至少一条 fact 或 counter_evidence 引用 KSM 证据证实部署。<br>　代价：比第四节更严，可能卡住通过；U1-b 原意是「让部署可被引用」，并非「必须引用」。<br>**c**：只按第四节原文，由审查者自由判断 | **a** | 第 5 步判定、两名审查者的输入 | |

### 可逆默认（Agent 自定，一行理由）

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| R-1 | offset 与 duration | `offset=420 s`，`duration=1800 s`，`paymentFailure: off → 100%`，method 按 lab 实际类型（文档示例 Int）运行时校验 | 留出约 1-2 分钟 rollout，使「rollout 完成 → 生效」≥5 分钟；告警约需 rate 5m + `for 2m`（F11 PR-4 合同引 U2 实测「起病到 anomalous 约 4–6 分钟」）；1800 s 覆盖告警加一次 Run，`offset+duration ≤ 7200`（`:1992-2002`） |
| R-2 | 追问里的时刻 | R（patch 回执，UTC 秒精度）；答案记录 R、rollout 完成上下界与 flagd 生效上下界，审查容差 ±60 s 加 rollout 时长 | R 是 harness 唯一确定的时刻（`:1236`）；KSM 粒度 60 s |
| R-3 | rubric 正文位置 | `docs/testing/f3-competing-cause-rubric.md`：第四节原文加 D2、D5 的澄清；文首记录冻结日期与 sha256 | 与 `docs/testing/f6/f12/f13-acceptance-tests.md` 同目录；验收文档不进模型输入 |
| R-4 | 评测侧答案与可见刺激的位置 | 新目录 `evals/f3/competing-cause/`：`stimulus.json` 只放追问文字模板（占位符 `{released_at}`、`{release_id}`）；`answer.json` 放注入（service、operation、别名、flag、variant）、部署（namespace、deployment、变更类型、offset/duration 计划）、证据特征（复用 `payment-charge-error-span-v1` 的 any_of）、D5 的否定判据、R-6 前提。不放进 `evals/scenarios/` | 沿用评测套件「答案只放评测侧」（`evals/README.md`）；放在 `evals/scenarios/` 外，避免改动 D1 六场景集合与 `load_scenarios` 的调用方（1.5 节） |
| R-5 | 冻结的保证方式 | 任务记录写两份文件的 sha256；加一个钉住这两个 sha256 的测试，改动必须同时改测试，并在 PR 说明里写明是收紧；PR-3b 的 `summary.json` 记录同样的 sha256 | 「冻结后只能收紧」需要机械的拦截点 |
| R-6 | 场景前提（驱动在 PR-3b 实跑时逐项检查，PR-3a 只实现检查与 dry-run 打印） | ① 已登记 lab 占用；② 不存在 `fault-active.json`；③ flagd 11 个 flag 全 off；④ `ALERTS{alertname="CheckoutPlaceOrderErrorRatioHigh"}` 为空；⑤ 目标上没有未 closed 的同告警事故（F2 C3）；⑥ 过去 70 分钟内没有其他 checkout rollout，也没有 F11 发布观察在途；⑦ swap 高于停止线 | ⑤ 防 #299 合并后被附加、不开新 Run；⑥ 防第二个竞争原因，并防 F11 PR-4 合并后把发布异常事件附加进 F3 事故；⑦ 见项目记忆 |
| R-7 | 驱动如何复用 harness | 新脚本 `scripts/f3_competing_scenario.py`，直接 import `publish`、`scheduled_fault`、`restore_fault`、`reserved`、`occupation`；用自己的 ledger 子目录，不写 F11 批次日志；**禁止调用 `register` 与 #298 的 `webhook`** | 不改 `f11_u2_harness.py`（与 #298 零重叠）；不污染 F11 U2 汇总；不登记发布，Observer 与 F11 PR-4 就不会介入 F3 事故 |
| R-8 | 追问的发送方 | 驱动经 HTTP 调用产品控制入口（Basic Auth、Origin、`expected_generation=0`、幂等键），凭据从私有 env 读取，ledger 只存请求哈希与响应 | 走产品真实路径，审计可查；不直接写库 |
| R-9 | 描述的写法 | 在 310-316 行清单后追加 KSM 一句，并在 `expr` 参数描述里说明 KSM 的标签名（`namespace`、`deployment`、`replicaset`、`pod`、`owner_name`）；遵循 `deepseek-flash-prompt-tool-reference.md` 的写法；不改 fixture profile | 只改 otel-demo 模型可见面；fixture 自有 revision（`tools/fixture.py:83`） |
| R-10 | 第八节「Observer 在无关并发故障下的判定」 | 在 D1-a 与 R-7 下不适用（不登记发布），关闭该未确认项 | 只有 U1-c 路径才依赖它 |

## 4. 冲突核对与排期

`gh pr diff --name-only` 结果（2026-10-22）：

| PR | 状态 | 改动文件（节选） | 与 PR-3a 重叠 |
|---|---|---|---|
| #298 F11 PR-3 release intake | OPEN，BEHIND | `scripts/f11_u2_harness.py`（新增 `release_webhook_payload`、`send_release_webhook`、`webhook` 子命令；import、常量区与 parser/main 有 hunk）、`scripts/kind_lab.py`、`opspilot/release_intake.py`、`opspilot/web/app.py`、`tests/acceptance/test_f7_step1_entry_points.py` 等 | 按 D2-a 与 R-7 无重叠；F3 驱动只 import 未被改动的函数。按 D2-b 会与它同文件 |
| #299 F2 alert correlation | OPEN，BLOCKED（3 个检查 SUCCESS） | 迁移 0018、`persistence/*`、`web/service.py`、模板、F2 测试 | 无文件重叠。行为耦合：同告警附加（R-6 ⑤） |
| #303 F11 policy M=1500 | OPEN，BLOCKED | `release-calibrations.json`、F11 测试与任务记录 | 无 |
| #306 F11 PR-4、F2 B1/B2 合同（文档） | OPEN | 仅文档；PR-4 实施允许改的文件不含 `otel_demo.py`（PR-4 合同第 5 节） | 无。PR-4 改 `prompt_revision` 与输入 v6，与 PR-3a 正交 |

bump 对这些线 kind 真实 Run 的影响：

- **#299**：真实运行已在 10-22 07:46–08:33Z 完成，记录的 `tool_schema_revision` 是 `otel-demo-4d7168180749`（`poa-f2-c` 分支的 `summary.json`）。PR-3a 合并后，证据本身不失效（不重签）。只有当它为修复「断言 2 失败」而重跑、且重跑前 merge 了含 PR-3a 的 `main` 时，才会出现两种情况：同一库里残留的旧 Run 被新 worker 领取时 blocked；新 Run 的模型可见面不同，与前次不可比。
- **#298、#303**：不产生调查 Run，不受影响。
- **F11 PR-4 实施（未开工）**：它的真实运行发生在它自己合并 `main` 之后，按当时的 revision 跑即可。
- **F11 U2 校准与边界轮**：只用 Observer 与 harness，不跑调查 Run，不受影响。

排期建议：

1. PR-3a 现在即可开工，与 PR-2 并行，不必等 #298、#299、#303。
2. PR-3a 合并时，在任务记录与 PR 说明里写明新 revision；通知其他线在真实观察窗内不要 merge `main` 后重启 worker，否则库里残留的 queued、paused、waiting 状态 Run 会 blocked。这是 C3 §5 的预期行为，不是缺陷。
3. PR-3b 的顺序门槛：PR-1、PR-2、PR-3a，以及（按 lead 口述）F11 PR-4 全部合并后，才跑第 5 步。原因是 PR-4 改 `prompt_revision`，在 PR-3b 之后合入会让第 5 步证据作废（任务记录:44）。#299 合并后，R-6 ⑤ 成为硬前提。
4. 若用户在 D2 选 b，PR-3a 必须等 #298 合并后再基于新 `main` 修改 harness（不做 stacked PR），并重新预审 harness 部分。

## 5. dry-run 自检能证明什么、不能证明什么

**能证明**（离线、可在 CI 跑）：

- 计划与冻结答案一致：发布对象 `otel-demo/checkout`、flag 与 variant、offset ≥300 且 `offset+duration ≤ 7200`。
- 时间算术：按计划的告警延迟上下界，R 落在 `startsAt − 1h` 默认查询窗内。
- dry-run 不产生任何外部调用或写盘：测试替换 `subprocess`、`urllib`、`psycopg`，一旦被调用即失败。
- 渲染出的追问文字满足：长度 ≤8192；通过 `_reject_ambiguous_text`；`redact_credentials` 后不变；不含答案词（`payment`、`charge`、`paymentFailure`、各别名、`f3`、`unrelated`、`decoy`，不区分大小写）。
- rubric 与答案文件可解析，sha256 与冻结记录一致。
- 驱动的动作序列里没有 `register` 与 `webhook`。
- R-6 前提检查逐项被调用，dry-run 下打印「跳过」。

**不能证明**（留给 PR-3b 或 D3 的探测）：

- flag 的 method 与 variant 在 lab 上是否匹配。
- KSM 序列与标签是否实际存在（D3-b 可提前证明）。
- 告警是否触发、何时触发。
- 追问是否在第 1 轮送达真实 worker：代码路径已核对，可加一个 PG 确定性测试覆盖，见测试清单 8。
- rollout 是否成功，以及 rollout 期间 checkout 是否出现瞬时错误。
- 模型行为，以及 rubric 是否通过。
- 其他线的并发干扰。

## 6. PR 范围、需改文件、测试清单

**范围**：U1-b 描述 bump；R9 场景驱动与 dry-run；rubric 与评测侧答案冻结。不改 harness、输入版本、prompt、报告 schema、迁移、权限、`passes`；不跑 Run，不调模型。D3-b 若获批，额外做一次只读序列探测，证据入 `docs/evidence/f3-ksm-series-probe/`。

**需改文件**（按推荐方案）：

- `opspilot/tools/otel_demo.py`：仅 `TOOL_SCHEMAS` 中 metrics 描述与 `expr` 参数描述（266-327 行区间）。
- 新增 `scripts/f3_competing_scenario.py`（R-7、R-8、R-6；子命令建议：`plan`/`preflight`/`run`，全局 `--dry-run`）。
- 新增 `evals/f3/competing-cause/stimulus.json`、`evals/f3/competing-cause/answer.json`（R-4）。
- 新增 `docs/testing/f3-competing-cause-rubric.md`（R-3）。
- `docs/tasks/2026-10-17-f3.md`：PR-3a 进展、新 revision、两份 sha256、D1–D5 裁决（由 lead 写；合同修订号递增为 r2）。
- `docs/development.md`：F3 驱动用法一节（含「先 web、后追问、再起 worker」的运行顺序）。
- 测试：见下（验收测试由全新上下文作者写）。

**验收测试应覆盖的断言**（只列可观察行为，不涉实现）：

1. 描述 bump：`TOOL_SCHEMA_REVISION != "otel-demo-4d7168180749"`，并等于按 `_tool_schema_revision` 规则重算的值；`PROJECTION_REVISION`、`prompt_revision`、`context_policy_revision` 不变。
2. 描述内容：metrics 描述包含 D3 定下的每个 KSM 序列名与标签名；不包含未确认的序列名（按 D3 结果）；明确说明 KSM 用 `namespace`/`deployment` 等标签而非 `service_name`；traces 工具描述字节不变。
3. 只读与授权不变：注册表动词仍为只读；无新连接器或凭据；`promql_problem` 对 KSM 表达式（如 `kube_replicaset_owner{namespace="otel-demo",owner_name="checkout"}`）返回 None。
4. 答案隔离：`stimulus.json` 与渲染后的追问文本不含 `answer.json` 中任何 service、operation、别名、flag、variant 字符串（不区分大小写）。驱动向工作台发出的 HTTP 请求体（替身捕获）不含 `answer.json` 的这些字段，可借鉴 canary 做法（`eval_c_support.py:32`）。`evals/scenarios/` 的场景集合未变。
5. 冻结：rubric 与答案文件的 sha256 等于任务记录登记值。
6. 计划一致：dry-run 输出包含 checkout 发布与 `paymentFailure off→100%`，offset ≥300，`offset+duration ≤7200`；R 在默认查询窗内的算术成立。
7. dry-run 无外部副作用：kubectl、flagd、HTTP、DB 均未调用；不创建 ledger、不写 `fault-active.json`、不写 `run-starts.jsonl`。
8. 追问通道（PG 确定性测试，可选但推荐）：告警投递建事故后、领取前发 `follow_up` → Run 仍为同一个，代际 +1，输入行 `kind=follow_up`；用替身模型跑第 1 轮时，请求消息里有 `investigation_inputs`，其 `text` 等于追问文字；输入版本仍为 v4 或 v5，不出现 v6。
9. 驱动不调用 `register`、`webhook`（静态或替身断言），追问请求使用 `expected_generation` 与幂等键，凭据不出现在 ledger 或 stdout。
10. 前提检查：R-6 每一项失败时，驱动拒绝开始并给出可区分的原因码（替身环境下逐项构造）。

**验证**：定向 pytest 加 `make check`（由实现者在非观察窗时执行）；独立审查用 Codex 全新会话。本 PR 不需要 LangSmith trace（不触碰调查 loop、报告校验与恢复路径，与 PR-1 同理）。

## 7. 未确认项

- 「F11 PR-4 必须早于 F3 PR-3b」的出处：仓库与 #306 都未找到，按 lead 口述采纳。
- KSM 中 `kube_replicaset_created`、`kube_deployment_metadata_generation` 等序列是否存在（D3）。
- paymentFailure 的 flagd method（文档示例是 Int，未核对）。
- checkout 配置 rollout 期间是否出现瞬时 PlaceOrder 错误：F11 U2 的 healthy 轮应有数据，本次未查。
- 「领取前追问 → 第 1 轮可见」只核对了代码路径，未实测。
- #300（P7 r3）的回放是否依赖当前 `tool_schema_revision`。
- 告警从故障生效到触发的实际延迟：只有 F11 U2 的「起病到 anomalous 4–6 分钟」可参照，那是 Observer 判定，不是 Alertmanager。
