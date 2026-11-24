# 批 4 租机（2026-11-23）

性质：有界真实运行，不是生产证明。DeepSeek 真调，宿主进程拓扑；每 Run 只有产品默认的防循环上限，没有额外预算。提示词没有改。证据先按 B4-15 留在运行 checkout，全部阶段结束后由 lead 原样复制入库。

## 主机

| 项 | 值 |
|---|---|
| 供应商 | Vultr |
| 规格 | 8 vCPU / 31 GB 内存 / 150 GB 磁盘，amd64 |
| 系统 | Ubuntu 24.04.5 LTS，时区 UTC |
| 开机时间（`uptime -s`） | 2026-11-23 14:28:07 UTC |
| 主机 ED25519 指纹 | `SHA256:+uNhrB7p9Z6aHPS2X0YlEMtLNT5E41zCvgCR25VjjpY` |
| 提交 S（运行 checkout `main`） | `e199727e`（docs: record round 2 results for next batches） |
| 单价 | 以账单为准 |

## 检查点 1：每个 PR / 条款一次真实 Run（B4-15）

拓扑：未合并的三个 PR 在主机上从 `e199727e` 依次合并到本地分支 `local/cp1-merge-2026-11-23`（worktree `/home/opspilot/poa-cp1`，HEAD `47419bca`，未推送）：#827 → #825 → #826，三次合并都没有冲突。第 4–8 项在运行 checkout `e199727e` 上跑。驱动里没有的自定义问题用仓库外的辅助脚本 `/home/opspilot/rental-logs/cp1/cp1_run.py`。它复用 #666 驱动的 PG、`web_env`/`worker_env`、环境隔离检查、`/intake/ui` 提交、`run_freeze.collect`、`trace_roots` 和 `lab_evidence.freeze`，没有改仓库代码；做法与 11-20 的 B3 辅助脚本相同。K8s 用 kind lab 的只读身份（`tmp/m1-kind-lab/k8s-investigator.env`）；Prometheus/Jaeger 指向不可达地址（H-11）。ES 是本次新起的容器 `cp1-es`，镜像为 CI 同一 digest `5bfdf431…`，开启安全认证，在 127.0.0.1:30920。产品只读 key 由 `scripts/es_readonly_identity.py` 用 elastic 基本认证创建。LangSmith 项目是 `opspilot-lab-cp1-2026-11-23`（长期保留），驱动冒烟用的是驱动自己的项目。

| # | 条款 | Run | 结果 | 判定 |
|---|---|---|---|---|
| 1 | PR #827 空 K8s 列表 | `01c47189-de6a-5072-87c2-7ab09f9df94c` | completed，4 次模型请求 | 通过，见 [pr827-k8s-empty-list](pr827-k8s-empty-list/run.md) |
| 2 | PR #825 重试反馈列出全部失败检查 | `d414df91-7555-5847-815a-da2f0688366b`，另重放 2 份 r3 终答 | completed，9 次模型请求；重放符合预期 | 通过，见 [pr825-retry-feedback](pr825-retry-feedback/run.md) |
| 3 | PR #826 trace 的 tool_calls | `f65b6206-94fa-5412-a71c-a1bdfdb3b08b` | completed，17 次模型请求 | 通过，见 [pr826-trace-tool-calls](pr826-trace-tool-calls/run.md) |
| 4 | #779 回看窗口（PR #820） | 30 天 `06967f3e-4d42-5388-9248-d3fee24c65bf`；默认 `e5a4db30-61ba-5d08-9e5f-5bf0c696be35` | 都是 completed | 通过，见 [779-lookback](779-lookback/run.md) |
| 5 | #696 managedFields（PR #822） | `060637a5-b20a-571c-ac8c-4d317169710e` | completed，4 次 | 通过，见 [696-managed-fields](696-managed-fields/run.md) |
| 6 | #808 ES 集群工具（PR #823） | `c0bf2ac2-3e58-505d-a948-4a69d173fda3`（另有第 7 项那次） | completed，3 次 | 通过，见 [808-es-cluster-tools](808-es-cluster-tools/run.md) |
| 7 | #809 驱动（PR #824） | `4b3b1031-3463-5e51-b660-aef862d0bf3e` | completed，judge A | 通过，见 [809-driver-smoke](809-driver-smoke/run.md) |
| 8 | #574 Pod env 脱敏（PR #805，B1-D15） | `43e35f51-5c76-5126-95f9-a405611f7911` | completed，5 次 | 通过，见 [574-env-redaction](574-env-redaction/run.md) |

DeepSeek 用量（9 个 Run 合计，按 summary.json 的 tokens 块）：prompt 1,876,820（缓存命中 1,672,704），completion 65,696。各 Run 的数字见各自的 run.md。

凭据扫描：结果见本文件末尾「扫描」一节。

自建夹具（命名空间 `cp1-np-empty`、`cp1-mf`、`cp1-redact`，ES 索引 `cp1-billing-logs`、`cp1-replicated`、`cp1-orders`）用完已删除；`cp1-es` 容器保留，留给后续阶段使用。kind lab 没有停止或重启，`kind_lab.py health` 在 15:24Z 为 ok。

## 扫描

2026-11-23 15:27Z 用 `/home/opspilot/rental-logs/cp1/cp1_secret_scan.py` 扫描本目录 20 个文件，共 10 个值：rental.env 的 DEEPSEEK_API_KEY、LANGSMITH_API_KEY、ZAI_API_KEY，ES 工程侧 key 与 elastic 密码，产品只读 ES key，K8s 调查 token，以及第 8 项的 2 个金丝雀。结果 0 命中；正对照（仓库外临时文件，内含其中一个值）1 命中。值本身没有打印。URL、端点和 LANGSMITH_WORKSPACE_ID 不算凭据，没有计入。

## 冒烟与全量（冻结提交 F `51257e79`，即 #827、#825、#826 合并后的 main）

| 阶段 | 时间（PT，11-23） | 结果 | 记录 |
|---|---|---|---|
| 冒烟（不计分） | 08:06–08:23 | 7 个 Run 都 completed。162 不带常驻说明为 B，拼上说明后为 A；185、280、235、254、275 都是 A；235 的 `time_box_limited: true` 首次在真实 Run 中出现 | [smoke/run.md](smoke/run.md) |
| Holmes 题集 OpsPilot 侧（57 题 × 3） | 08:23–11:41；补判 12:45–12:48 | **163/171**。同一 53 题为 151/159，11-20 为 133/159 | [full/run.md](full/run.md) |
| Holmes 侧 #808 新增 4 题 × 3 | 12:50–12:56 | 12/12；加上沿用的 11-20 结果，Holmes 共 162/171 | 同上 |
| SREGym Lite 17 × 3 | 12:57–21:26 | **38/51**，11-20 为 43/51 | [full/sregym/run.md](full/sregym/run.md) |

- Z.ai judge 余额耗尽：10:00–12:44 PT 期间返回 429 / code 1113。Holmes 题集 70 遍事后补判；Holmes 侧 4 题和 SREGym 都推迟到余额恢复后才开始。
- 硬停时间是 11-24 08:30 PT，全部在此之前完成。
- DeepSeek 合计（检查点 1、冒烟、Holmes 题集、SREGym）：prompt 208,399,927（命中 194,360,704，未命中 14,039,223），completion 3,874,245。按 11-21 读到的 deepseek-flash 闲时价估算约 $5.0：未命中 $0.15、命中 $0.003、输出 $0.60，均为每百万 token。当前价目与峰谷时段未确认。Holmes 侧与 judge 的用量没有统计。
- 最终凭据扫描（11-24 05:28Z，`/home/opspilot/rental-logs/b4/secret_scan.py`）：本目录 293 个文件，27 个值。值包括 rental.env 三个 key、两套 ES 的工程侧 key、elastic 密码和只读 key、K8s 调查 token、lab 的 Prometheus 口令（6 个账号与 4 个 env）、两个 webhook/部署 token、workbench 事件 token，以及第 8 项的 2 个金丝雀。结果 0 命中，正对照 1 命中。SREGym 每遍签发的 111 个只读令牌与入口密钥另扫一遍，0 命中，正对照 1 命中。
- 偏离：检查点 1 结束后执行 `git worktree remove poa-cp1`，删掉了第 1–3 项被忽略的原始 ledger（`tmp/lab-ledgers`）。它们的 summary.json 和重放输出（`/home/opspilot/rental-logs/cp1/replay-out/`）都还在。
