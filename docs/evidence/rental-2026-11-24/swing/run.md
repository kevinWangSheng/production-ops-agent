# 批 4 租机补跑：SREGym 摆动题与 p12 Valkey 回复（2026-11-24 08:45–10:52Z，00:45–02:52 PT）

性质：有界真实运行，不是生产证明，也不是排行榜成绩（`SELF_RUN_NOT_LEADERBOARD`）。主机、集群、judge 与 11-23 全量相同（Vultr `opspilot-tool-1`，`kind-sregym`，Z.ai glm-5.3-flashx，`--cluster-path filter_proxy_readonly`，`--trace lab`），见 [11-23 记录](../../rental-2026-11-23/full/sregym/run.md)。提示词与产品代码在补跑期间未改。

- 命令：[s_runner.sh](s_runner.sh)，每遍调用一次驱动，轮次 `swing1124`；规则与 11-23 全量相同（env_broken 重跑一次、中止重试一次、ANSWER_LEAK 全停）。[runner.log](runner.log)：12 遍全部 completed / ok，没有重跑、中止或 env_broken。
- 目的一（#831）：在 11-23 全量的同一冻结提交 F `51257e79` 上重跑掉分的三题，各 3 遍，分开「随机波动」和「两次全量之间合并的改动」。证据 `swing-f/`。
- 目的二（#830）：在当前 main `3d702d65`（含 #833 记录回复原文）上跑 p12 3 遍，取 harness 建只读 Valkey 账号失败时的回复。证据 `p12-main/`。

## 结果

| 题 | 11-20（`e9550ecd`） | 11-23 全量（F） | 本次补跑（F） |
|---|---|---|---|
| p01 | 2/3 | 0/3 | **2/3** |
| p05 | 3/3 | 1/3 | **3/3** |
| p14 | 3/3 | 2/3 | **3/3** |
| p12（main） | 0/3 | 0/3 | 0/3 |

- 同一提交 F 上三题合计从 3/9 回到 8/9，和 11-20 的 8/9 相同。11-23 的下降更像是运行间波动（模型与 judge 都有随机性），不是 `e9550ecd..51257e79` 之间的改动造成的。每题只有 6 遍（F 上两次合计），样本小，这一点仍是推断。F 上两次合计：p01 2/6、p05 4/6、p14 5/6。
- p12：3 遍的 `valkey_account` 都是 `ADMIN_FAILED:RedisReplyError`，回复原文为 `ERR Protocol error: unauthenticated multibulk length`。Valkey 对未认证连接限制多段命令的长度，harness 以 default 用户、不带 AUTH 发出的 `ACL SETUSER` 超过了这个限制，说明被测 Valkey 要求认证。harness 该如何认证留在 #830 第 2 步决定。p12 仍按设计披露 `VALKEY_READONLY_ACCOUNT_FAILED` 并继续跑完。

## 用量与扫描

- DeepSeek（12 遍，按 summary.json 的 tokens 块）：prompt 36,776,653（命中 34,653,568，未命中 2,123,085），completion 519,557。按 11-21 读到的闲时价估算约 $0.73，当前价目未确认。judge 用量没有统计。
- 凭据扫描（10:5xZ，`/home/opspilot/rental-logs/b4/secret_scan.py`，与 11-23 同一份 27 个值）：本目录 12 个 summary.json，0 命中，正对照 1 命中。每遍签发的只读令牌与入口密钥这次没有另扫。
- 运行 checkout：F 在 `/home/opspilot/production-ops-agent`，main 在主机 worktree `/home/opspilot/poa-main-1124`；原始 ledger 不入库。
