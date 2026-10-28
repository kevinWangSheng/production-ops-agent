# #242 真实压缩 Run 与 PG 重建证据（2026-10-24）

两次有界真实 DeepSeek Run（`deepseek-flash`），参数相同。驱动是 `scripts/m1_live_runner.py`，走真实 `InvestigationRunner`、一次性 PostgreSQL 17 和产品 `otel-demo` 工具 profile（ledger、control、transport 都是产品代码）。合同：`~/.agent/handoffs/opspilot-prereview-242-2026-10-24.md`（U-1 = A，R-1..R-6）。本证据单独断言，不作 F2 `passes` 门槛。

**Run 2 是主证据。** Run 1 跑完后，summary 字段改名以对齐独立验收测试（`pg_rebuild` 改为 `offline_rebuild`，逐行明细移到 `compaction_rows`），于是用改名后的脚本、同样的参数又跑了一次。Run 1 的 summary 保持原样（旧字段名），不改写。两次都通过。

## 系统做了什么

1. 脚本用 `--context-tokens 80000` 把这个 Run 的上下文窗口写进输入快照（产品默认与 web 不变），经 `DurableStore.accept` 建 Run。
2. Run 在真实模型下调查。工具轮次累积到阈值附近时，loop 发一次压缩请求；模型的摘要被接受后，之前的轮次被折叠，调查在新段 `ctx{n}` 继续。最后发布报告。
3. Run 结束后，脚本从 PG 读回这个 Run 的全部行，用产品的 `rebuild_transcript` 离线重建下一次模型调用的上下文。runner 恢复时走的也是这段代码。重建会逐行核对每个压缩行折叠了哪些 step，以及压缩前后每轮的快照哈希，不一致就报 `INCOMPATIBLE_STATE`。

Run 2 的 summary 字段：
- `compactions`：`{accepted, attempted}`，`attempted` 是 PG 里 `kind=compaction` 的行数，`accepted` 是其中被接受的行数。
- `compaction_rows`：每个压缩行的 `from_segment`、`to_segment`、`folded_step_ids`（数量），已提交行的顺序 `step_keys`，以及最后一个被接受压缩之后的轮次数 `rounds_after_last_accepted`。
- `offline_rebuild`：`{segment, compactions, error}`。重建失败时 `segment`、`compactions` 为 null，`error` 写错误码。
- `offline_rebuild_detail`：重建的其余读数，包括重建后的已采纳证据 id。
- `citations`：报告引用的每个证据 id 来自哪个段、是否被已接受的压缩折叠、重建后是否仍在已采纳证据中。

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
- 窗口取值（R-2）：先做只估算、不发请求的预演。otel-demo 的固定前缀估算约 5.1k token。`76536`、`80000`、`84000` 三档的压缩阈值分别约 10.4k、13.7k、17.5k token，单个工具结果的存根上限分别约 2.8k、3.6k、4.6k token。#309 用 74036 时压缩后放不下（`CONTEXT_EXHAUSTED`），用 76536 时两次压缩后发布。本次取 80000，给摘要多留约 3k token。

## Run 2（主证据）

Run `c184919d-f035-498f-9c41-4b50d587a4fb`：`published`，`run_state=completed`，12 次 HTTP（9 个模型轮次加 3 次压缩，用满 `--model-requests 12`），2026-10-24 08:00:47–08:03:20Z。

- [summary.json](c184919d-f035-498f-9c41-4b50d587a4fb/summary.json)
- [LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/b74a3f98-179e-4d31-a2bd-45e04d425f75/r/00000000-0000-0000-b6f5-f563ce652682?poll=true)（项目 `opspilot-lab-242-compaction`，根 run 已读回，`dropped_spans=0`）

已提交行的顺序：`ctx0:round-1..5`、`ctx0:compact-1`（accepted，折叠 5 个轮次）、`ctx1:round-6`、`ctx1:compact-2`（accepted，折叠 1 个）、`ctx2:round-7`、`ctx2:compact-3`（accepted，折叠 1 个）、`ctx3:round-8..9`、`conclusion:g0:ctx3:round-9`。

### 合同第 6 节断言

| # | 断言 | 结果 | 依据（summary 字段） |
|---|---|---|---|
| ① | 至少 1 个 `kind=compaction`、`accepted=true` 的已提交行，之后至少 1 个 `ctx{n}:round-*` 行 | 通过：3 个压缩行全部被接受；最后一个之后有 2 个 `ctx3:round-*` | `compactions={"accepted": 3, "attempted": 3}`、`compaction_rows.rows`、`compaction_rows.rounds_after_last_accepted=2` |
| ② | Run 结束后从 PG 离线重建成功 | 通过：重建不报错；segment 是 `ctx3`，等于最后一个被接受压缩的 `to_segment`；重建得到的压缩次数 3 等于被接受的行数；每个压缩行折叠的 step 和每轮的快照哈希都核对通过 | `offline_rebuild={"segment": "ctx3", "compactions": 3, "error": null}` |
| ③ | 发布且报告通过产品校验；每个 claim 的 evidence id 都在已采纳证据中；至少一个引用来自压缩前的轮次 | 通过：结果是 `published`；报告 19 条 claim 共引用 12 个证据 id，全部在重建后的已采纳证据中；其中 10 个来自被压缩折叠的轮次（`ctx0` 7 个、`ctx1` 1 个、`ctx2` 2 个），另 2 个来自 `ctx3` | `verdicts.status`、`citations.cited_not_delivered=[]`、`citations.cited_from_folded_rounds=10` |
| ④ | LangSmith trace 链接、冻结 summary、余额差 | 通过：链接见上方，余额见「费用」一节 | `trace.url`、`trace.read_back="found"` |

照实记录、不判失败：
- 压缩后有没有重查压缩前的同一调用：**未发生**。PG 行里 18 个工具调用，同工具同参数的组合有 18 个，没有重复。这项行为以 #309 的确定性测试为准。
- 18 次工具结果：12 次 `ok`、2 次 `no_data`、4 次 `error`。替身只提供固定的合成序列，有些查询它不支持。

## Run 1（同参数，summary 用旧字段名）

Run `bceaa5fb-8fa6-4f93-b1cd-e98dd848162f`：`published`，`run_state=completed`，12 次 HTTP，07:55:12–07:57:20Z。[summary.json](bceaa5fb-8fa6-4f93-b1cd-e98dd848162f/summary.json)，[trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/b74a3f98-179e-4d31-a2bd-45e04d425f75/r/00000000-0000-0000-7668-55e32636fe4d?poll=true)。

- 2 个压缩行都被接受：`ctx0:compact-1` 折叠 6 个轮次，`ctx1:compact-2` 折叠 1 个；最后一个之后有 3 个 `ctx2:round-*`。
- 离线重建：`pg_rebuild.ok=true`，segment 是 `ctx2`，压缩次数 2。
- 报告 17 条 claim 引用 12 个证据 id，全部在重建后的已采纳证据中，其中 11 个来自被折叠的轮次。
- 20 个工具调用互不相同；结果为 13 次 `ok`、3 次 `no_data`、4 次 `error`。
- 四条断言同样全部通过。字段名对应：`pg_rebuild` 对应现在的 `offline_rebuild` 加 `offline_rebuild_detail`，`compactions` 里的逐行明细对应现在的 `compaction_rows`。

两次都没有 `CONTEXT_EXHAUSTED`，没有失败轮。Run 2 是因为字段改名而重跑，不是因为失败。

## 局限

- **工具后端是本机替身，不是 kind 实验环境**（R-1）。替身 `scripts/m1_otel_stub.py` 返回合成的 Prometheus `query_range` 与 Jaeger 数据，每个请求固定延迟 1.5 s。所以这两次 Run 证明的是压缩路径和压缩行的 PG 往返，不证明对真实遥测的调查质量。
- PG 往返是 Run 结束后的**离线重建**，不是进程被杀后的真实认领（这是 U-1 选 A 的已知代价）。重建用的是 runner 恢复时同一段 `rebuild_transcript`；kill/resume 本身见 F2 D 的 `docs/evidence/f2-kill-resume-live/`。
- 每个 Run 只有一个 attempt。没有触发预算交接，所以没有走 `--follow-up` 续跑。
- 原始 ledger 在被忽略的 `tmp/lab-ledgers/` 下，没有入库；summary 里记有它的 sha256。

## 费用

- DeepSeek 余额（扣费有延迟）：
  - Run 1 前，07:54:51Z：95.77 CNY
  - Run 1 后，07:59:13Z：95.66 CNY
  - Run 2 前，08:00:34Z：95.63 CNY
  - Run 2 后，08:03:44Z：95.62 CNY；08:04:22Z：95.56 CNY
  - 两次合计：95.77 → 95.56 CNY，差额 0.21 CNY
- `known_cost_cny_upper`：Run 1 为 0.462 CNY，Run 2 为 0.509 CNY。这是不计缓存折扣的上界。
- 同一账户可能有其他调用方，读数时扣费也可能还没结清，所以余额差只是近似值。
