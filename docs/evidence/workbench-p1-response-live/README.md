# 工作台重设计 P1 回复合同：有界真实 Run（2026-11-17）

真实 DeepSeek Run（`deepseek-flash`），驱动 `scripts/m1_live_runner.py --message`，走真实 `InvestigationRunner`、本 worktree `tmp/` 下的一次性 PostgreSQL 17（端口 55442）和脚本自带的 fixture 工具（固定返回 `http_errors_rate=0.042`）。合同：`~/.agent/handoffs/opspilot-workbench-redesign-prereview-2026-11-17.md`（A3–A6、B4–B5、C1–C9）。

## 系统做了什么

1. 第一个 Run 按告警问题调查（`--model-requests 3`）。fixture 数据只有一条序列，两次都以「报告合格但自评不完整」交接（`INCOMPLETE_INVESTIGATION`，即合同 D7a）。
2. 脚本代替还不存在的 P2 接力写入续接状态：把交接的 Run 写成 cancelled（A10），追加一条 `message` 输入「你好」，插入一个同代际的排队 Run，输入是 C3 续接加 `scope_facts["conversation"]={"floor":0,"mode":"report_optional"}`，事故的 `current_run_id` 指过去。结论、lifecycle、代际不动。
3. runner 再领一次。续接 Run 每一轮的输入里「你好」带 `"new": true`，紧跟一条回复说明；步骤行记 `reply_instruction_sha256`，`prompt_revision` 仍是 `prompt-replay-candidate-8688f6e52275`。

## Run 2（主证据）：answered

- [summary.json](0e671822-9de4-4100-9143-9724fbb9e2a3/summary.json)；[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/51ed7670-4877-4ac3-9aa9-ebee43eb8f2f/r/00000000-0000-0000-a51e-c539b5ab6b19?poll=true)（项目 `opspilot-lab-workbench-p1`，续接 Run 的根 run 已读回，`dropped_spans=0`）。
- 续接 Run `33d0ebfc…`：第 1 轮调了一次工具（可见内容为空，`note=null`、`replies=[]`），第 2 轮不调工具、返回 `m0-response-v1`：一条回复 `to_input_seq=1`，中文问候加现状说明，引用的两个证据编号都属于本 Run（没有 `unsupported`）；同时附了一份自评不完整的报告，按 A3 记 `report_rejected_reason=INCOMPLETE_INVESTIGATION`。
- 结局：结论步骤 `execution=answered`、`handoff=false`，Run 行 `completed`，事件流以 `run_completed`（`published:false`）结束；事故结论（本例为空）、lifecycle、代际不变（`conclusion_unchanged: true`）。
- 5 次 HTTP（首个 Run 3 次，续接 Run 2 次），已知费用上限 0.096019 CNY。时间 2026-11-17 07:53–07:54 PT。

## Run 1：RESPONSE_INVALID（保留原样）

- [summary.json](40050c82-f9ab-45c1-af56-9a231cfe9990/summary.json)；[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/51ed7670-4877-4ac3-9aa9-ebee43eb8f2f/r/00000000-0000-0000-8a00-9687fcd02b60?poll=true)。
- 续接 Run 第 2 轮不调工具，却返回了工具轮用的 `{"note":…,"replies":[]}`，没有回答「你好」。按 A3 交接 `RESPONSE_INVALID`，Run 行 `waiting_human`，结论不动。4 次 HTTP，0.06144 CNY。
- 原因在回复说明的措辞：没说清 note 格式只用于调工具的轮次、空 replies 不算回答；`report_optional` 的说法还假定已有发布的报告（D7a 没有）。改了这三处（`reply_instruction_sha256` 从 `abecf9ff…` 变为 `3c97bbac…`）后跑 Run 2。回复说明不进 `prompt_revision`（合同 C5），所以这次改动不影响任何无消息 Run 的字节。

另有一次更早的尝试（首个 Run 同样以 INCOMPLETE 交接，脚本当时只在「已发布」后才发消息，续接没有发生，2 次 HTTP，0.025583 CNY），摘要未保留。三次合计费用上限约 0.183 CNY（token × 单价）。

## 运行

```
env PYTHONPATH=. M0_ENV_FILE=<private .env> \
  OPSPILOT_LAB_DSN="host=127.0.0.1 port=55442 dbname=m0_budget user=m0_lab" \
  OPSPILOT_TRACE=lab OPSPILOT_HEALTH_BIND= OPSPILOT_LEDGER_DIR=tmp/lab-ledgers \
  M1_ACCEPTANCE_OUT=docs/evidence/workbench-p1-response-live \
  .venv/bin/python scripts/m1_live_runner.py --model-requests 3 --lab-round workbench-p1 \
    --message "你好" --message-mode report_optional
```

凭据只由进程从私有 env 文件读取，没有打印。原始 ledger 在被忽略的 `tmp/lab-ledgers/`，摘要里只留它的 sha256。

## 没有覆盖的

- 已发布结论上的「你好」（D3）和 `reply_only`（A6）没有真实 Run：fixture 数据下首个 Run 出不了完整报告。这两条由 `tests/acceptance/test_workbench_conversation.py` 的脚本化模型场景覆盖。
- 续接状态由脚本写入，不是 P2 的接力；fixture 工具的次数没有记入 Run 行（`tool_operations_used=0` 是这个脚本执行器的既有行为）。
