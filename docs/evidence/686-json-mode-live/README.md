# #686 每轮开 JSON 模式：有界真实 Run（2026-11-17）

真实 DeepSeek Run（`deepseek-flash`），和工作台 P1 的证据同一套接法：`scripts/m1_live_runner.py --message`，真实 `InvestigationRunner`、本 worktree `tmp/` 下的一次性 PostgreSQL 17（端口 55447）、脚本自带的 fixture 工具（固定返回 `http_errors_rate=0.042`）。代码为 `feature/686-json-mode-every-round`：`loop.py` 每个调查轮 `json_mode=True`，压缩请求仍为 False；U2/U3 默认值、D6 重试提示。合同：[任务记录](../../tasks/2026-11-17-r3-report-json-mode.md)、[决定表 r1](../../tasks/2026-11-17-r3-report-json-mode-contract.md)。

- [summary.json](19cf2060-31ee-4757-81c7-666c10ce21d5/summary.json)（一次脚本调用：首个 Run 加续接 Run）。
- LangSmith 项目 `opspilot-lab-686-json-mode`：首个 Run [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/cfde3246-6a59-4eb2-9ee2-7045f43d5cb5/r/00000000-0000-0000-e0e3-ae8da2244da6?poll=true)；续接 Run [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/cfde3246-6a59-4eb2-9ee2-7045f43d5cb5/r/00000000-0000-0000-a5b3-994b17fc42df?poll=true)（脚本读回根 run，`dropped_spans=0`）。
- 请求参数：读回项目里全部 4 个模型 span，`opspilot.model.json_mode` 都是 `true`（两次工具轮同时带 `tools`，两次不带工具的轮次不带）。没有发生压缩（`compactions: 0`）。
- `prompt_revision` 仍是 `prompt-replay-candidate-8688f6e52275`。
- 时间 2026-11-17 10:48–10:49 PT；4 次 HTTP，`prompt_tokens` 12534、`completion_tokens` 5660，已知费用上限 0.077032 CNY（token × 单价）。

## 首个 Run（无消息，`report_required`）：报告合格，自评不完整

| 轮 | 内容 | finish | prompt / completion / 缓存命中 |
|---|---|---|---|
| 1 | 调 1 次工具，可见内容为空串 | `tool_calls` | 2142 / 168 / 1664 |
| 2 | 纯 JSON 的 `m0-report-v3`（无前置文字、无围栏），`incomplete` + `inconclusive`，4 条 claim 都带 `evidence_ids`，含 `next_steps` | `stop` | 2765 / 2252 / 2304 |

结局：报告通过解析与引用检查，按自评不完整交接 `INCOMPLETE_INVESTIGATION`（P1 合同 D7a，与 P1 证据的首个 Run 相同：fixture 只有一条序列）。工具轮空白内容不算失败、原样存进步骤行（R6）。

## 续接 Run（消息「你好」，`report_optional`）：RESPONSE_INVALID

| 轮 | 内容 | finish | prompt / completion / 缓存命中 |
|---|---|---|---|
| 1 | 调 1 次工具，可见内容为空串（`note=null`、`replies=[]`） | `tool_calls` | 3543 / 93 / 1792 |
| 2 | 不调工具，返回一个 JSON 对象，键是 `note`、`replies`、`report`，**没有** `schema_version: m0-response-v1` | `stop` | 4084 / 3147 / 2944 |

第 2 轮的 `replies` 里有一条回答 `to_input_seq=1` 的中文回复，引用的证据编号属于本 Run；`report` 是一份 `m0-report-v3`。因为顶层不是 `m0-response-v1` 包装，P1 的 `split_response` 把整个对象当裸报告校验，失败；又没有有效回复，按 A3 交接 `RESPONSE_INVALID`，Run 行 `waiting_human`，结论、lifecycle、代际不变（`conclusion_unchanged: true`）。

这和 P1 证据里的 Run 1 是同一类失败（终答轮用了工具轮的 note 形式），P1 改回复说明措辞后 Run 2 成功；本次在同一份回复说明（`reply_instruction_sha256` `3c97bbac…`）下又出现了混合形式。单次样本，看不出是否由 JSON 模式引起。按 R7 本 PR 不改 P1 的解析与回复说明模板；是否处理交 lead 决定。

后续：这次 `RESPONSE_INVALID` 促成了 Q2（合同 r2，提交 `9baabc3a`）——不带 `schema_version`、但含 `replies` 键的对象按 `m0-response-v1` 包装处理，回复说明文字不变。Q2 落在本证据之后，这里没有任何真实 Run 跑过 Q2；由 r3 租机冒烟覆盖。

## 运行

```
env PYTHONPATH=. M0_ENV_FILE=<private .env> \
  OPSPILOT_LAB_DSN="host=127.0.0.1 port=55447 dbname=m0_budget user=m0_lab" \
  OPSPILOT_TRACE=lab OPSPILOT_HEALTH_BIND= OPSPILOT_LEDGER_DIR=tmp/lab-ledgers \
  M1_ACCEPTANCE_OUT=docs/evidence/686-json-mode-live \
  .venv/bin/python scripts/m1_live_runner.py --model-requests 3 --lab-round 686-json-mode \
    --message "你好" --message-mode report_optional
```

凭据只由进程从私有 env 文件读取，没有打印。原始 ledger 在被忽略的 `tmp/lab-ledgers/`，摘要里只留它的 sha256。每轮的内容形态与 token 数取自本次 PG 的 `opspilot_steps` 行；`json_mode` 取自 LangSmith 读回的模型 span。

## 没有覆盖的

- 强制终答重试（D6 的新提示）、空白终答（`EMPTY_REPORT`）、压缩请求都没有在真实 Run 里出现；这些由脚本化模型测试覆盖。
- fixture 工具，不是真实 OTel；只跑了一次，不代表格式失败率。
