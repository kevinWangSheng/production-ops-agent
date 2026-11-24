# 批 4 SREGym Lite 全量（17 题 × 3 遍，2026-11-23 20:57Z – 11-24 05:26Z，12:57–21:26 PT）

性质：有界真实运行（冻结提交 F `51257e79`，运行 checkout 无改动），DeepSeek 真调，SREGym judge 用 Z.ai glm-5.3-flashx，`--cluster-path filter_proxy_readonly`，`--trace lab`。不是生产证明，也不是排行榜成绩（披露码 `SELF_RUN_NOT_LEADERBOARD`）。

- 集群：`kind-sregym` 在本阶段前新建（上游 `kind/setup_kind_cluster.sh`，4 节点加 Calico，11:44 PT），kubeconfig `/home/opspilot/.kube/sregym.yaml`，`driver prepare` 结果为 prepared。开跑前停了 otel lab（`kind_lab.py stop`）。
- 命令：主机 `/home/opspilot/rental-logs/b4/g_runner.sh`，每遍调用一次驱动。规则：env_broken 重跑一次；中止重试一次；同一遍再中止就跳过并披露；ANSWER_LEAK 立即全停；08:30 PT 硬停。`run-logs/` 里有 runner.log、rows.jsonl（每遍一行：类别、是否通过、token、trace）和 runner 脚本。
- 结果：**38/51（74.5%）**。11-20 为 43/51（84.3%）。每题明细见 `sregym-lite-full/scores.json`。

| 题 | 本次 | 11-20 |
|---|---|---|
| p01 | 0/3 | 2/3 |
| p05 | 1/3 | 3/3 |
| p06 | 2/3 | 2/3 |
| p08 | 2/3 | 2/3 |
| p12 | 0/3 | 0/3 |
| p13 | 1/3 | 1/3 |
| p14 | 2/3 | 3/3 |
| 其余 10 题 | 各 3/3 | 各 3/3 |

- 没过的 13 遍都提交了完整报告：Run 都到了终态，submitted=true，run_status complete，没有超时、中止或 env_broken。这些是判分没过，不是环境问题。p01、p05、p14 的退步原因不明（未确认），每题只有 3 遍，没有做摆动补跑。
- **p12 只读 Valkey 账号（#809、B4-U6）**：3 遍都是 `VALKEY_READONLY_ACCOUNT_FAILED`，`valkey_account.code` 为 `ADMIN_FAILED:RedisReplyError`。harness 以 default 用户、不带 AUTH 执行 `ACL SETUSER` 时收到错误回复（`scripts/sregym_eval/agent_entry.py:686-700`），回复内容没有记录，具体原因未确认。p12 因此照旧没有只读账号，0/3，已按设计披露并继续跑。
- 披露与重跑：
  1. p01 t1 前两次是 env_broken。原因是 `/home/opspilot/.config` 在主机准备时归 root 所有，helm 无法建配置目录，`repo add` 报 permission denied，Loki chart 装不上。21:10Z 把这个目录改归 opspilot（不递归），之后每一遍都正常。
  2. 主循环结束后用 `--redo p01:t1` 重跑。这一遍完整跑完了，但冻结时崩溃：`set_aside` 把旧 summary 挪走后留下了空目录，`lab_evidence.freeze` 要求目录不存在（`exist_ok=False`），抛 FileExistsError。删除空目录后再跑一遍，completed，没过。p01 t1 共跑了 4 次，前两次是环境问题、第三次是驱动问题，计分只用最后一次。
- DeepSeek（51 遍）：prompt 148,432,033（命中 139,524,480），completion 1,947,523。另有 p01 t1 冻结崩溃那次的用量没有记录。SREGym judge 用量没有统计。
