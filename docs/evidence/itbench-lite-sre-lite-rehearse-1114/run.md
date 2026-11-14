# #632 修复后的中断恢复重新彩排（#619、#497）

类别：集成运行（真实 DeepSeek、回放组件、真实 judge），不计分。#497 规则：修复只影响恢复路径，所以只重新彩排这一项。提交 F `5f731318`（#632 修复，PR #634）。时间 UTC，2026-11-14。前一次彩排与缺陷见 [小批记录](../itbench-lite-sre-lite-small-1114/run.md)。

- 每轮 1 题（S2）× 1 次，新轮次；驱动输出直接重定向到文件（不经 `tee`）；中断用驱动所在 tmux 前台的 Ctrl-C；恢复用同一条命令。lab 停着。
- 通过标准（lead）：恢复后 trial completed，`itbench_lite.history` 为空（没有 `PG_DIRECTORY_EXISTS`），`trial-1/stale/<时刻>/` 里是被中断的 `chance-1`；中断后与恢复后端口 29110、29190、29186、29118、29143、29180 无监听，`itbl-*` 容器为 0。

| 轮次 | 中断时刻 | 中断落点（驱动 traceback） | 中断后清理 | 恢复结果 | stale |
|---|---|---|---|---|---|
| `lite-rehearse-1114` | 22:43:04（开跑 81 秒后） | 等 Run 期间：`driver.py:942` `b.sleep(cfg.poll_seconds)` | 立即干净：端口无监听、容器 0、无残留进程；无 `CLEANUP_FAILED` | 22:52:00 退出 0，completed，`history: []`，`chance: 1`，judged | `stale/20261114T224416854923/chance-1` |
| `lite-rehearse2-1114` | 22:53:42 | 等 Run 期间：`driver.py:935` `run_state` | 同上 | 23:00:13 退出 0，completed，`history: []`，`chance: 1` | `stale/20261114T225438924959/chance-1` |
| `lite-rehearse3-1114` | 23:01:02（`chance-1/pg` 出现后 20 秒，灌数据中：Jaeger 容器在、Prometheus 尚未起） | 灌数据中：`driver.py:798` `_parallel` 等待 `future.result()` | 中断后 30 秒内驱动仍在、5 个端口仍监听、1 个容器：驱动等并行灌数据的线程结束后才清理；23:01:59 前全部干净，无 `CLEANUP_FAILED` | 23:09:50 退出 0，completed，`history: []`，`chance: 1`，load 56.5 s，灌完到投递 122.3 s，judged | `stale/20261114T230328317843/chance-1` |

三轮都满足通过标准。stale 路径都在主机 `tmp/itbench-lite/<轮次>/S2/trial-1/` 下（git 忽略的原始目录，2026-11-14 用 `ls -d` 核对）。版本 `prompt-replay-candidate-k8s-0282ee7d0e7a` / `otel-demo-ext-d467c4b436b0`，提交 `5f731318`，`dirty: false`。trace：[rehearse](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/c7dcf4b6-cfc9-42c4-8a97-22ff792b3a8a/r/00000000-0000-0000-427e-741b4fe032e7)、[rehearse2](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/85e0ce6e-e8bc-4747-9ad3-aa4e8cd80d36/r/00000000-0000-0000-c398-1e50ba93b50d)、[rehearse3](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/75551d5e-3d25-4ed6-b297-481d9a13691e/r/00000000-0000-0000-f181-7aa50199f1b4)。

观察（不是缺陷）：灌数据中按 Ctrl-C，驱动要等正在跑的灌数据线程结束（本次约 30–57 秒）才开始清理；期间端口与容器仍在。全量中断后应等驱动进程退出再查端口，不要在它退出前判定「没清理」。

冻结摘要：`itbench-lite-sre-lite-rehearse{,2,3}-1114/S2/trial-1/summary.json`（复制前在主机上对 token 与 11 个已知秘密值逐个搜索，0 命中）。
