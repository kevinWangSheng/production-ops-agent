# PR #725（集群名注入）261/262 真实 Run（线 L，2026-11-20）

性质：有界真实运行。worktree /home/opspilot/poa-725，HEAD ddcf5cee（feature/722-cluster-name，已合入 main 50a507e2），使用该提交自带的 #666 驱动：`round --questions 261,262 --trials 1 --trace lab`，真实 ES（l-es，127.0.0.1:30920），端口 31000 段，judge 开启。

- **两题都没有调查**：Run 261 `7833cfac-6610-525c-8ae1-dac47a2bfe6d`（cluster_name production-eu-west-2）和 Run 262 `a251de3d-1550-5ef4-8490-21c8450857aa`（cluster_name production）都是 state blocked，handoff 原因 INCOMPATIBLE_STATE，模型请求 0 次，没有 trace root，驱动归类 no_report（REPORT_REJECTED_BY_PRODUCT）。
- 原因是驱动只给 worker 设了 OPSPILOT_CLUSTER_NAME，web 没有设：`scripts/holmes_evals/driver.py` 的 worker_env 第 518–519 行会写它，web_env 不写。结果 web 建 Run 时记下的 prompt_revision 是 `prompt-replay-candidate-k8s-1137251eea4d`（未设名字的值），而 worker 的槽位是 `prompt-replay-candidate-k8s-cluster-01c909abf29b`（worker.log 第 1 条 slot 日志），领取时版本不一致，于是被拦。这正是 PR 正文里写的"web 与 worker 设置不一致时 claim 报 INCOMPATIBLE_STATE"，产品行为符合合同，问题出在驱动接线。
- summary：`holmes-evals-l-pr725-261-262/{261_time_window_gap_external_data,262_ambiguous_cluster_reference}/trial-1/summary.json`。原始日志在主机 /home/opspilot/rental-logs/L/tmp/l-pr725-261-262/。
- 未完成：带集群名提示词的真实调查 trace。需要先修驱动，让 web_env 也设 OPSPILOT_CLUSTER_NAME，再重跑。

## 重跑（HEAD 537bf461，驱动修复后，round `l-pr725-r2`）

web 和 worker 现在都拿到 OPSPILOT_CLUSTER_NAME。worker 4 条 slot 日志与两个 Run 记录的 prompt_revision 都是 `prompt-replay-candidate-k8s-cluster-01c909abf29b`，版本一致，不再出现 INCOMPATIBLE_STATE。从两遍的 PG 副本读 Run input，scope_facts 里有 `cluster_name`（261 是 production-eu-west-2，262 是 production）。

| 题 | Run | 结果 | 模型请求 / prompt(命中) / completion | 判分 | trace |
|---|---|---|---|---|---|
| 261 | 995e2dd5-71b0-56ec-83e5-571c201ec7cf | waiting_human / handed_off INCOMPLETE_INVESTIGATION；有报告对象但未发布，驱动归 no_report | 11 / 219,519(197,888) / 6,390 | 无（no_report） | https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/a61c6b6d-fa8f-4e34-b70a-c524093335ce/r/00000000-0000-0000-93df-2b292f81deee |
| 262 | 3b238635-ee8e-5269-8cc4-51a5ac3d6561 | completed，报告引用 3 条 elasticsearch_search 证据 | 16 / 333,412(310,912) / 9,145 | A 通过（运行时判分） | https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/a61c6b6d-fa8f-4e34-b70a-c524093335ce/r/00000000-0000-0000-e68a-9fbcca4d05e5 |

K8s、Prometheus、Jaeger 的错误来自驱动故意给的不可达地址（H-11）。summary：`holmes-evals-l-pr725-r2/{261_…,262_…}/trial-1/summary.json`。
