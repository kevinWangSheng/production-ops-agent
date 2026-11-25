# #830 验证：SREGym p12 注入前建只读 Valkey 账号（2026-11-25 06:20–07:02Z，11-24 22:20–23:02 PT）

性质：有界真实运行，不是生产证明，也不是排行榜成绩（`SELF_RUN_NOT_LEADERBOARD`）。DeepSeek 真调，SREGym judge 用 Z.ai glm-5.3-flashx，`--cluster-path filter_proxy_readonly`，`--trace lab`。

- 主机：Vultr `opspilot-tool-1`（8 vCPU / 32 GB，11-24 新开），运行 checkout `/home/opspilot/production-ops-agent`，提交 `179c1e30`（含已合并的 #830 注入前钩子 `scripts/sregym_eval/opspilot_sregym_hooks.py`），每遍 `git.dirty=false`。SREGym 上游 `ac60c642`，apps 子模块 `647cd558`。
- 集群：开跑前停 otel lab（`kind_lab.py stop`，集群保留）。`kind-sregym` 用上游 `KIND_CLUSTER_NAME=sregym bash kind/setup_kind_cluster.sh` 新建（4 节点加 Calico，11-24 18:21:56–18:22:59Z），kubeconfig `/home/opspilot/.kube/sregym.yaml`；`driver prepare` 结果为 prepared，开跑前复查一次仍为 prepared、4 节点 Ready。
- 命令：[run-logs/r_runner.sh](run-logs/r_runner.sh)，每遍调用一次驱动；规则同 11-23 全量（env_broken 重跑一次、中止重试一次、ANSWER_LEAK 全停）。先跑一遍 p01 作前置检查（轮次 `pre1124`，不计入 p12），再跑 p12 ×3（轮次 `r1124`）。[run-logs/runner.log](run-logs/runner.log)。

## 结果

| 遍 | 钩子结果（注入前） | 入口记录 `valkey_account` | Valkey 工具调用 | 披露 | judge |
|---|---|---|---|---|---|
| p12 t1 | created，06:38:52Z | created，`created_before_fault_injection: true` | `redis_info` 1、`redis_config_get` 1，全部 ok | `VALKEY_READONLY_ACCOUNT_BY_HARNESS` | 通过（1.0） |
| p12 t2 | created，06:47:35Z | 同上 | `redis_info` 3、`redis_config_get` 1，全部 ok | 同上 | 通过（1.0） |
| p12 t3 | created，06:57:25Z | 同上 | `redis_info` 2、`redis_config_get` 2，全部 ok | 同上 | 通过（1.0） |

- p12 **3/3**（11-20、11-23、11-24 均为 0/3，当时账号没建成）。三遍都是 Run completed、报告已提交、run_status complete，没有重跑、中止或 env_broken。
- 账号是否活过故障：入口在注入后用只读账号自己 `AUTH` + `ACL GETUSER` 自查，三遍都通过；产品工具调用也都返回 ok。三遍都没有出现 `VALKEY_READONLY_ACCOUNT_FAILED`，即没观察到故障重置账号。
- 驱动生成的 `scores.json` 分母是整轮 51 遍（只跑了 p12），`main` 的 3/51 没有意义，按题看 p12 为 3/3。
- trace：LangSmith 项目 `opspilot-lab-sregym-lite-r1124`，三遍链接在各 `summary.json` 的 `trace.roots`，读回均为 found。

## 前置检查（p01，不计分）

- 目的：确认钩子模块对每一题都会加载，而非 p12 时不会拖垮其他题（PR #849 审查意见 O1）。
- 第一次（11-24 18:24–18:25Z）两遍都是 env_broken：SREGym judge 预检报 `AuthenticationError`（Z.ai 401），原因是主机 `rental.env` 里的 Z.ai key 不对，与钩子无关。lead 修好 key 后，把这两遍的记录挪出证据目录（留在主机 `~/rental-logs/r1124/pre-envbroken/`），重跑。
- 重跑（11-25 06:20–06:34Z）：completed，`agents.yaml` 含 `hooks: opspilot_sregym_hooks:HOOKS`，main.log 里没有任何导入错误，judge 预检通过，故障注入与诊断阶段都正常走完，报告已提交。judge 判不过（0.0），属于评分结果，不是环境问题。p01 不是 Valkey 题，记录里没有 `valkey_account`，符合预期。

## 用量与扫描

- DeepSeek（按 summary.json 的 `run.tokens`）：p12 三遍 prompt 5,355,401（命中 5,039,744，未命中 315,657），completion 89,055；含 p01 共四遍 prompt 8,913,743（命中 8,422,272，未命中 491,471），completion 145,690。单价沿用 11-22 记录（deepseek-flash 每百万 token 命中 $0.003、未命中 $0.15、输出 $0.60；周一至周五 06–10 UTC 为高峰、价格翻倍），本次在周三 06:20–07:02 UTC，按高峰价算：四遍约 **$0.37**，其中 p12 约 $0.23。当前价目未重新确认。两次 env_broken 在 judge 预检就退出，没有 DeepSeek 调用。judge 用量没有统计。
- 凭据扫描：主机上逐个比对 `rental.env` 里 8 个值（长度 ≥ 12）与本目录 4 个 summary.json，API key、数据库 URL 都是 0 命中；`LANGSMITH_WORKSPACE_ID` 4 命中，它是 trace 链接里的组织 ID，不是凭据，往期证据同样包含。gitleaks（`make secrets-branch`）在 p12 三个 summary 报 6 处 generic-api-key，经核对是 valkey 镜像标签 `9.0.1-alpine3.23` 和幂等标签 `sregym-r1124-p12-tN`，与往期租机证据同类误报；按同样做法在 `scripts/check_secrets.py` 加限定路径与精确值的例外后，`BRANCH_SECRETS_CLEAN`，全仓 `SECRET_SCAN_PASSED`。
- 轮询 p01 进度时 grep 到了 main.log 中几行注入阶段的输出，没有据此改动任何东西，也没有写入本记录。
