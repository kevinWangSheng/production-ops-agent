# #242 真实压缩 Run 与 PG 重建证据（2026-10-24）

一次有界真实 DeepSeek Run（`deepseek-flash`），驱动是 `scripts/m1_live_runner.py`，走真实 `InvestigationRunner`、一次性 PostgreSQL 17 和产品 `otel-demo` 工具 profile（ledger、control、transport 都是产品代码）。合同：`~/.agent/handoffs/opspilot-prereview-242-2026-10-24.md`（U-1 = A，R-1..R-6）。本证据单独断言，不作 F2 `passes` 门槛。

## 系统做了什么

1. 脚本用 `--context-tokens 80000` 把这个 Run 的上下文窗口写进输入快照（产品默认与 web 不变），经 `DurableStore.accept` 建 Run。
2. Run 在真实模型下调查：6 个工具轮次后上下文接近阈值，loop 发一次压缩请求，模型的摘要被接受（`ctx0:compact-1`，折叠 6 个轮次），之后在新段 `ctx1` 继续；又一轮后第二次压缩被接受（`ctx1:compact-2`），在 `ctx2` 再走 3 轮，最后发布报告。
3. Run 结束后，脚本从 PG 读回这个 Run 的全部行，用产品的 `rebuild_transcript`（与 runner 恢复时走的同一段代码）离线重建下一次模型调用的上下文。重建会逐行核对压缩行折叠的 step、以及压缩前后每轮的快照哈希，不一致即 `INCOMPATIBLE_STATE`。结果写进冻结 `summary.json` 的 `compactions`、`pg_rebuild`、`citations` 三个字段。

## 运行

- 命令（凭据只从私有 env 文件读取，不打印）：

```
env -u OPSPILOT_OTEL_TOKEN -u OPSPILOT_OTEL_PROMETHEUS_USERNAME -u OPSPILOT_OTEL_PROMETHEUS_PASSWORD \
  PYTHONPATH=. M0_ENV_FILE=<private .env> \
  OPSPILOT_LAB_DSN="host=127.0.0.1 port=55951 dbname=m0_budget user=m0_lab" \
  OPSPILOT_TRACE=lab OPSPILOT_TOOL_PROFILE=otel-demo \
  OPSPILOT_OTEL_PROMETHEUS_URL=http://127.0.0.1:29342 \
  OPSPILOT_OTEL_JAEGER_URL=http://127.0.0.1:29342/jaeger/ui \
  OPSPILOT_LEDGER_DIR=tmp/lab-ledgers M1_ACCEPTANCE_OUT=docs/evidence/242-compaction-live \
  .venv/bin/python scripts/m1_live_runner.py --tool-profile otel-demo --tool-parallelism 2 \
    --model-requests 12 --context-tokens 80000 --lab-round 242-compaction
```

- 替身：`.venv/bin/python scripts/m1_otel_stub.py --port 29342 --latency-seconds 1.5`。
- 数据库：本 worktree `tmp/` 下 initdb 的一次性 PG 17，端口 55951，已 `opspilot.schema migrate`（head `0020_release_comparisons`）。用完已停止。
- 窗口取值（R-2）：先做只估算、不发请求的预演：otel-demo 固定前缀估算约 5.1k token；`76536` / `80000` / `84000` 的压缩阈值分别约 10.4k / 13.7k / 17.5k，单个工具结果存根上限约 2.8k / 3.6k / 4.6k。#309 在 74036 时压缩后放不下（`CONTEXT_EXHAUSTED`），76536 时两次压缩后发布；本轮取 80000，给摘要多留约 3k token。

## 结果

Run `bceaa5fb-8fa6-4f93-b1cd-e98dd848162f`，`published`，`run_state=completed`，12 次 HTTP（10 个模型轮次 + 2 次压缩，用满 `--model-requests 12`），2026-10-24 07:55:12–07:57:20Z。

- [summary.json](bceaa5fb-8fa6-4f93-b1cd-e98dd848162f/summary.json)
- [LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/b74a3f98-179e-4d31-a2bd-45e04d425f75/r/00000000-0000-0000-7668-55e32636fe4d?poll=true)（项目 `opspilot-lab-242-compaction`，根 run 已读回，`dropped_spans=0`）

已提交行顺序（`compactions.step_keys`）：`ctx0:round-1..6`、`ctx0:compact-1`（accepted，折叠 6）、`ctx1:round-7`、`ctx1:compact-2`（accepted，折叠 1）、`ctx2:round-8..10`、`conclusion:g0:ctx2:round-10`。

### 合同第 6 节断言

| # | 断言 | 结果 | 依据（summary 字段） |
|---|---|---|---|
| ① | 至少 1 个 `kind=compaction`、`accepted=true` 的已提交行，之后至少 1 个 `ctx{n}:round-*` 行 | 通过：2 个被接受的压缩行；最后一个之后有 3 个 `ctx2:round-*` | `compactions.accepted=2`、`compactions.attempted=2`、`compactions.rows`、`compactions.rounds_after_last_accepted=3` |
| ② | Run 结束后从 PG 离线重建成功 | 通过：不抛错；segment `ctx2` 等于最后一个被接受压缩的 `to_segment`；重建得到的压缩次数 2 等于被接受行数；两行的折叠 step 与每轮快照哈希都核对通过 | `pg_rebuild.ok=true`、`pg_rebuild.error=null`、`pg_rebuild.segment="ctx2"`、`pg_rebuild.compactions=2` |
| ③ | 发布且报告通过产品校验；每个 claim 的 evidence id 都在已采纳证据中；至少一个引用来自压缩前的轮次 | 通过：`published`；报告 17 条 claim 共引用 12 个证据 id，全部在重建后的已采纳证据中；其中 11 个来自被接受压缩折叠的轮次（`ctx0` 8 个、`ctx1` 3 个） | `verdicts.status`、`citations.cited_not_delivered=[]`、`citations.cited_from_folded_rounds=11` |
| ④ | LangSmith trace 链接、冻结 summary、余额差 | 通过：见上方链接与下方费用 | `trace.url`、`trace.read_back="found"` |

照实记录、不判失败的项：

- 压缩后有没有重查压缩前的同一调用：**未发生**。PG 行里 20 个工具调用（同工具、同参数）互不相同。该行为以 #309 的确定性测试为准。
- 20 次工具结果：13 次 `ok`、3 次 `no_data`、4 次 `error`（替身只提供固定的合成序列，部分查询它不支持）。
- 本轮没有 `CONTEXT_EXHAUSTED`，没有失败轮，没有重跑。

## 局限

- **工具后端是本机替身，不是 kind 实验环境**（R-1）。替身 `scripts/m1_otel_stub.py` 返回合成的 Prometheus `query_range` 与 Jaeger 数据，每请求固定延迟 1.5 s。所以本 Run 证明的是压缩路径和压缩行的 PG 往返，不证明对真实遥测的调查质量。
- PG 往返是 Run 结束后的**离线重建**，不是进程被杀后的真实认领（U-1 A 的已知代价）。重建用的是 runner 恢复时同一段 `rebuild_transcript`；kill/resume 本身见 F2 D 的 `docs/evidence/f2-kill-resume-live/`。
- 本 Run 只有一个 attempt；没有触发预算交接，所以没有走 `--follow-up` 续跑。
- 原始 ledger 在被忽略的 `tmp/lab-ledgers/` 下，没有入库；summary 里记有它的 sha256。

## 费用

- DeepSeek 余额：07:54:51Z 为 95.77 CNY；07:57:33Z 与 07:58:06Z 仍为 95.77 CNY（扣费有延迟），07:58:52Z 为 95.75 CNY，07:59:13Z 为 95.66 CNY。按最后一次读数，差额 0.11 CNY。同一账户可能有其他调用方，且扣费在读数时可能还未结清，所以这个差额只是近似值。
- 本 Run `known_cost_cny_upper` 0.462 CNY（prompt 122317、completion 22204 token，不计缓存折扣的上界）。
