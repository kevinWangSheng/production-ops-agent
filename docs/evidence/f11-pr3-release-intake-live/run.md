# F11 PR-3 发布来源接入：kind 有界真实运行（`f11-pr3-live-1`）

- 日期：2026-10-22（UTC 20:47–20:51），kind-opspilot-m1 / otel-demo；代码 `68221a2`（`feature/F11-pr3-release-intake`）
- 合同：[任务记录 PR-3 r1–r3](../../tasks/2026-10-12-f11-post-release.md)、[PR-3 预审](../../tasks/2026-10-21-f11-pr3-contract.md)（U4/U5/U6、P3-01..03）
- 冻结摘要：[`summary.json`](summary.json)；原始 ledger 在主仓库 `tmp/lab-ledgers/f11-pr3-live-1/`（不入库），各文件 sha256 见摘要 `artifact_sha256`
- 性质：接入链路取证。校准清单为空（0 条 policy），不是正式发布观察；F11 `passes` 仍为 `false`

## 环境

- 工作台：本 worktree 的 `python -m opspilot.web serve`，`127.0.0.1:8080`；`OPSPILOT_WEB_DSN` 为 `opspilot_web` 成员登录角色；`OPSPILOT_EVENT_TOKENS` 只含 alertmanager 与 release-deployer 两个 token 的哈希；`OPSPILOT_TARGET_IDENTITIES` 为 lead 核实的 `tmp/184-step3/targets.json`；自动观察关闭，Observer 未启动。进程环境里没有 `KUBECONFIG`。
- 数据库：新建的 PostgreSQL 17.9 临时实例（initdb，端口 55651），迁移到 `0017_web_suspension_grants`，不碰共享的 55561/55431。
- release-deployer token：调用 `kind_lab.webhook_token(RELEASE_TOKEN_FILE)` 和 `workbench_event_env(..., actor="release-deployer")` 生成（与 `kind_lab.py up` 新增的步骤相同，没有跑完整的 `up`），文件权限 600；token 不进证据。
- 宿主读数（开跑前 → 结束后）：可用内存 45% → 44%，swap 已用 6604 → 6500 MiB，pageouts 15486522 → 15487693，checkout p95 95 → 96 ms，frontend span 速率 2.25/s。

## 时间线（UTC）

| 时间 | 动作 | 结果 |
|---|---|---|
| 20:49:02.036 | harness `release`：`kubectl patch` checkout，加 `OPSPILOT_U2_RELEASE_ID=f11-pr3-live-20261022-1` | generation 16→17，RS `ff6bd6fdc`→`645dc77999` |
| 20:49:02–04 | harness `watch` | 新 Pod Ready，旧 Pod 消失，after 身份补齐 |
| ~20:49:20 | U5 基线 s1（滚动完成后） | 308 个对象 |
| 20:49:31.977 | harness `webhook`（真实 HTTP，release-deployer） | 201 `created`，observation `9d684d46…`，状态 `pending` |
| 20:49:35.644 | 同一载荷再发一次 | 200 `replayed`，observation id 相同 |
| 20:49:46.048 | 同一载荷，alertmanager token | 403 `RELEASE_ACTOR_REQUIRED` |
| 20:49:46.052 | release-deployer，`resource_uid` 未登记 | 422 `TARGET_UNRESOLVED` |
| 20:50:18.545 | 以 web 角色调用 `ReleaseStore.authorize_release` | rejected `RELEASE_POLICY_UNCALIBRATED`，状态保持 `pending` |
| ~20:50:30 | U5 快照 s2 | 与 s1 相比无差异 |

## 断言

1. **PASS 真实 HTTP 登记**：请求 sha256 `8cf5f516…af379b`，响应 201，sha256 `a10df7f5…f8d17c`。库里有 1 条 ReleaseObservation：`released_at` 等于 harness 记录的 patch 回执时间 20:49:02.035831Z，deadline 为 +3600s，`min_tracking_seconds` 1500，profile `otel-demo-checkout@b72bbe2e30be`。对应 1 条投递审计（sequence 1，`created`，actor `release-deployer`），其 `raw_sha256` 等于请求哈希。auxiliary 中的 Deployment UID、generation 17、两个 RS UID 和两个 pod-template-hash 与 harness ledger 一致。
2. **PASS 重放幂等**：响应 200，内容为 `replayed`，sha256 `a09e781b…3af2`。主体仍是 1 条，内容不变；新增审计 sequence 2（`replayed`），raw 与 semantic 哈希都和首投相同。
3. **PASS 授权照实拒绝**：校准清单有 0 条 policy，授权返回 rejected `RELEASE_POLICY_UNCALIBRATED`，写入一条 rejected 审计，状态和代际都不变，没有建 session。注意：本分支没有 HTTP 授权路由，这一步直接以 web 登录角色调用 PR-2 的 `authorize_release` 存储原语；`calibrated_policy_revision` 传的是探针值，清单为空，任何值都匹配不上。
4. **PASS 负例**：alertmanager actor 返回 403 `RELEASE_ACTOR_REQUIRED`；目标未登记返回 422 `TARGET_UNRESOLVED`。两者都没有新建主体，也没有投递审计行（审计行数保持 2）。
5. **PASS（以 kind 集群为范围）零发布门写入**：基线 s1 在 harness 发布、watch 和 `rollout status` 完成之后才取。s2 在 webhook 两次、两个负例和授权之后取。比对范围是 23 类策略面对象，共 308 个：Deployment、StatefulSet、DaemonSet、ReplicaSet、ControllerRevision、HPA、PDB、NetworkPolicy、Ingress、IngressClass、Service、ConfigMap（含 flagd）、Job、CronJob、admission webhook 与 policy、CRD、RBAC。比对字段为 spec 哈希、labels/annotations 哈希、generation，以及非 status 的 managedFields（manager、操作、时间）。结果：新增 0、删除 0、变化 0。实验写入的 actor 证据：checkout Deployment 的 managedFields 中有 `kubectl-patch` / Update / 2026-10-22T20:49:02Z，与 harness 记录的 R 吻合；s0→s1 只有 checkout Deployment（generation 16→17）、旧 RS 缩容和新 RS `checkout-645dc77999` 出现变化，均来自这次 patch。
   - 限制：kind apiserver 没有开启审计日志，actor 证据只来自 managedFields 和 generation。s0 拍摄时漏了 `--show-managed-fields`，s1 和 s2 都包含。集群外的 CI/GitHub 门没有运行时快照，只有静态证据：`opspilot/` 中没有 kubectl、Kubernetes 客户端或 GitHub API 调用，唯一的 `subprocess` 是迁移用的 pg_dump；工作台进程也没有 kubeconfig。
- 零查询：`opspilot_release_query_results`、observation session、incident、Run 全部为 0。

## 未做与说明

- 没有做 409 身份冲突、413、503 的真实注入（合同测试已覆盖，本次不要求）。
- 没有调用模型，因此没有 LangSmith trace，费用为 0。
- 实验结束后，checkout 保留新的环境变量值，与 U2 各轮的做法相同。
- 已停止本次启动的工作台（PID 75563）和临时 PG；数据保留在 worktree 的 `tmp/f11-pr3-live/pg`；kind 占用已 release。
