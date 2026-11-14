# ITBench-Lite 小批与中断彩排：6 题 × 1 次（#619、#497）

类别：集成运行（真实 DeepSeek、回放组件、真实 judge），不计分（#616）。依据 [租机手册](../../tasks/2026-11-13-rental-runbook.md) 阶段 5；主机见 [租机记录](../rental-2026-11/run.md)，冒烟见 [冒烟记录](../itbench-lite-sre-lite-smoke-1114b/run.md)。时间 UTC，2026-11-14。

- **提交 P**：`e6be89ad`（S' 加 #628 提示词改动）。版本 `prompt-replay-candidate-k8s-0282ee7d0e7a` / `otel-demo-ext-d467c4b436b0` / `ctx-ctx-policy-v1-a30e65ea52f4`，6 个 summary 都一样，`itbench_lite.git.dirty` 全为 false。
- 轮次 `lite-small-1114`，档位 `otel-demo-ext`，分档与词表沿用冒烟时生成的 `tmp/itbench-lite/inputs/`（P 没有改生成器）。
- **lab 已停**：阶段 5 不需要 lab，20:44:41 `python3 scripts/kind_lab.py stop`（退出 0），释放内存（used 3042 → 941 MiB）。所以本轮灌入耗时是在 lab 停着时测的，和冒烟（lab 开着）不完全可比。
- 命令：`.venv/bin/python -m scripts.itbench_lite.driver round --round lite-small-1114 --dataset <数据集根> --vocabulary-dir tmp/itbench-lite/inputs/vocab --scenarios S2,S4,S11,S12,S14,S18 --trials 1 --env-file ~/rental-private/lite.env`（judge 凭据与 `ITBENCH_EVALUATIONS_DIR` 从父进程环境继承）。

## 5.1 挑题

| 档 | 题 | 规则 |
|---|---|---|
| `指标+trace`（14 题） | S11、S12、S14 | 按编号取前 3，跳过冒烟用过的 S1、S5 |
| `故障窗无 trace`（21 题） | S2、S4、S18 | 按编号取 S2、S4；第 3 题换成 S18/S38 里编号小的 S18（数据残缺路径） |

## 每题结果

| 题 | 档 | 退出 / status | load_seconds | data_end_to_delivery | 作废 / 基础设施失败 | target_fallback | outcome | conversion | judge | 轮次 | prompt（缓存命中）/ completion | 工具错误 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S2 | 无 trace | 0 / completed | 57 | 122 | 0 / 0 | false | completed，report | null | judged | 23 | 1,372,060（1,201,024）/ 58,668 | traces_search RESULT_TOO_LARGE ×2 |
| S4 | 无 trace | 0 / completed | 76 | 141 | 0 / **1** | false | completed，report | null | judged | 28 | 2,351,280（2,250,624）/ 54,037 | fetch_pod_logs RESULT_TOO_LARGE |
| S11 | 指标+trace | 0 / completed | 91 | 156 | 0 / 0 | false | completed，report | null | judged | 22 | 1,760,346（1,646,464）/ 48,119 | traces_search_by_tags RESULT_TOO_LARGE |
| S12 | 指标+trace | 0 / completed | 76 | 142 | 0 / 0 | **true** | completed，report | null | judged | 16 | 702,592（659,584）/ 37,186 | kubernetes_tabular_query RESULT_TOO_LARGE |
| S14 | 指标+trace | 0 / completed | 63 | 129 | 0 / 0 | false | completed，report | null | judged | 15 | 853,948（781,568）/ 34,043 | traces_search RESULT_TOO_LARGE |
| S18 | 无 trace | 0 / completed | 66 | 132 | 0 / 0 | false | completed，report | null | judged | 25 | 1,314,718（1,186,304）/ 39,136 | metrics_range_query INVALID_PARAMS；traces_search RESULT_TOO_LARGE ×2 |

驱动整轮退出码 0（21:35:23）。灌数据 57–91 秒，灌完到投递 122–156 秒，都远低于 4 分钟线；作废 0。假 server 加载 7–18 秒，开跑前 used 1024–1137 MiB，swap 0。分数只记录，不在这里列。LangSmith（read_back 全部 found）：[S2](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/64fc552b-7ea1-4246-977a-c469595acd00/r/00000000-0000-0000-7748-f5ccabe55ebe)、[S4](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/64fc552b-7ea1-4246-977a-c469595acd00/r/00000000-0000-0000-95a6-0e6b0fcaa483)、[S11](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/64fc552b-7ea1-4246-977a-c469595acd00/r/00000000-0000-0000-046f-2e1df00ba345)、[S12](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/64fc552b-7ea1-4246-977a-c469595acd00/r/00000000-0000-0000-a3a2-6a5c495769de)、[S14](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/64fc552b-7ea1-4246-977a-c469595acd00/r/00000000-0000-0000-f44c-1b0482005bdd)、[S18](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/64fc552b-7ea1-4246-977a-c469595acd00/r/00000000-0000-0000-4bf9-bdd3645af537)。

## 5.3 中断与 5.4 恢复（#497）

| 时刻 | 事件 | 证据 |
|---|---|---|
| 20:45:08 | 开跑 | — |
| 20:52:13 | S2 那一行 JSON 打印（completed、judged） | 驱动 stdout |
| 20:55:26 | S4 进行中（`itbl-…-s4-t1-c1-prom` 与 `-jaeger` 在跑，正在灌数据），在驱动所在的 tmux 前台按 Ctrl-C | — |
| 20:56:11 | 检查：29110、29190、29186、29118、29143、29180 无监听；`itbl-*` 容器 0 个（含已停止的）；没有残留的驱动、web、worker 进程；唯一的 postgres 是 4b 的 lab 库（55431） | `ss -ltn`、`docker ps -a`、`ps` |
| 20:56:37 | 同一条命令重跑 | — |
| 20:56:5x | S2：`{"scenario": "2", "skipped": [1]}` | 驱动 stdout |
| 21:35:23 | S4 本次 completed，其余 4 题照常跑完，驱动退出 0；再查端口与容器，同样干净 | 同上 |

- **记录缺口**：中断那次驱动在 tmux 里经 `tee` 输出，Ctrl-C 同时打到了 `tee`，所以驱动收到 SIGINT 之后打印的内容没有留下；`CLEANUP_FAILED` 是否出现只能从端口、容器、进程都已清理干净间接判断。恢复那次改为直接重定向到文件。
- **与手册写法不同（类别 ①）**：手册说被中断 trial 的旧证据移到 `tmp/…/trial-1/stale/` 后重跑。实际上 `stale/` 没有出现；恢复后 S4 的第 1 次机会在原 `chance-1` 目录里起 PG，因为中断留下的 `chance-1/pg` 还在而失败，记为 `{"chance": 1, "kind": "infra", "code": "PG_DIRECTORY_EXISTS"}`，第 2 次机会（`chance-2`）才跑完。`_set_aside`（`driver.py:1130`）只移动证据目录，而机会编号每次从 1 开始（`driver.py:682`）。后果：每被中断一次，该 trial 就白白用掉 3 次机会中的 1 次；全量期间同一 trial 被中断两次以上就可能记为缺失。已起草缺陷 issue 交 lead。

## 反馈（按 #616 分类）

| # | 发现 | 证据 | 类别 |
|---|---|---|---|
| 1 | 恢复时被中断的机会目录没有清走，下一次机会撞 `PG_DIRECTORY_EXISTS`，用掉一次机会 | S4 `itbench_lite.history`；`driver.py:682`、`:1130` | ① |
| 2 | `RESULT_TOO_LARGE` 在 #628 之后仍然常见，6 题全有；除 trace 搜索外还出现在 `fetch_pod_logs`（S4）与 `kubernetes_tabular_query`（S12） | 各 summary 的 `tool_requests` | ④（lead 已决定检查点 4 之后不改） |
| 3 | `metrics_range_query` INVALID_PARAMS 一次（S18） | S18 `tool_requests` | ④ |
| 4 | S12 `target_fallback: true`（入口告警没有 `service_name`，手册已知风险） | S12 summary | 已知限制，记入全量说明 |

## 检查点 5

| 判定项 | 结果 |
|---|---|
| 无基础设施失败，或已开 issue 并处置 | 1 次（S4 恢复时 `PG_DIRECTORY_EXISTS`），已起草 issue；作废 0 |
| 每题 judge 正常返回 | 6/6 `judged: true` |
| 5.3：驱动退出、端口与容器干净 | 端口与容器干净；`CLEANUP_FAILED` 输出未能留存（见上） |
| 5.4：S2 `skipped: [1]`，被中断那题 completed，其余跑完，跑完后再查干净 | 满足 |
| 两档都跑到 | 两档各 3 题 |
| 反馈按阶段 3 分类处置；A7-14 停点 | ① 一项待 issue；④ 由 lead 决定不改，A7-14 不触发 |

冻结摘要：`S{2,4,11,12,14,18}/trial-1/summary.json`（复制前在主机上对 token 与 11 个已知秘密值逐个搜索，0 命中；sha256 与主机一致）。原始 ledger 在主机 `tmp/lab-ledgers/`，不入库。费用（token × 单价）未算。
