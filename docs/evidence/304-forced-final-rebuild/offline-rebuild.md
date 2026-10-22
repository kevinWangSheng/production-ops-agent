# #304 离线重建：Run 1b21375d 的真实行（D-U2 ①）

日期 2026-10-22。只读：数据目录副本上的临时 PG，所有查询在 `read_only` 连接里执行，没有写入。不调用模型，不连 kind。机器可读摘要见 [offline-rebuild.json](offline-rebuild.json)。

## 环境

- 数据来源：`poa-f2-c/tmp/pg-f2c-live`，用 `cp -a` 复制到会话 scratchpad，原目录没有改动、没有启动。临时 PG 17.9 只监听 `127.0.0.1:55724`，用完后停止并删除副本。
- Run `1b21375d-607b-51d9-9527-51992cde88c9`（incident `e4527304-…`，现为 `cancelled`），`input` 版本 `opspilot-investigation-input-v5`，`model_requests=100`，授权目标 `m0-otel-20260909`。该 Run 的工具结果都只指向这个目标，所以重建不会出现 revoked view（diverged），哈希校验对每一行都会执行。
- 对照代码：修复前为 `main` a7b296a（scratchpad 里 `git worktree add --detach`），修复后为 `fix/304-forced-final-rebuild` 8af441e。两边用同一个只读脚本，脚本在 scratchpad，通过 `PYTHONPATH` 切换代码。
- 重建参数：snapshot 用该 Run 的行，`control_generation` 取 incident 当前值 5，因此旧代际的行全部按历史处理，相当于 follow_up 之后的重建；`inputs` 取该 incident 的全部输入行；`authorized_targets={"m0-otel-20260909"}`。

## 行摘要（确认根因假设）

| 行 | 状态 | 代际 | 工具调用 | final | finish_reason | 报告校验 |
|---|---|---|---|---|---|---|
| ctx0:round-1 … round-7 | tool_result_committed | 0 | 2–3 | false | tool_calls | — |
| ctx0:round-8 | response_committed | 0 | 0 | false | stop | 能解析为 v2 报告，引用校验失败 → `REPORT_INVALID` |
| ctx0:round-9 | response_committed | 0 | 0 | **true** | stop | 能解析，引用校验失败（交接原因 `REPORT_INVALID`） |
| conclusion:g0:ctx0:round-9 | response_committed | 0 | — | — | — | — |

- 没有行带 `rejected_plan`。round-9 的 `input_watermark=0`。
- 结论：round-8 是一条非最终、无工具调用、校验不合格的回复，round-9 是 L1a/B3 的强制最终轮。预审中「第 9 轮只能是 L1a 强制最终轮」的推断由真实行**确认**。

## 修复前后的重建结果

| 代码 | 只重建 round 1–8 | 重建 round 1–9 | 整个 Run |
|---|---|---|---|
| main a7b296a | ok | `INCOMPATIBLE_STATE` | `INCOMPATIBLE_STATE` |
| 修复 8af441e | ok | ok | ok（next_round=10，last_round.final=true，delivered views 15） |

修复前，失败只出现在强制最终轮那一行，前 8 轮的哈希都能复现。这说明系统提示词、证据上下文和工具结果的重建本身没有问题，缺的只是 retry_feedback。

## round-9 哈希比对（修复分支）

用 `validated_report` 对 round-8 的已提交 content 重算，结果为 `REPORT_INVALID`，feedback 374 字符，开头是「The previous reply was not accepted as the final report (REPORT_INVALID)」。

| | sha256 |
|---|---|
| 库里记录的 `input_snapshot_hash` | `6ff95471636030b02bc6b697b37b5c33483e4e1fbf2ac0dfb8b53b744d06e729` |
| 重建：指令 + feedback + 覆盖摘要 | `6ff95471636030b02bc6b697b37b5c33483e4e1fbf2ac0dfb8b53b744d06e729`（**一致**） |
| 重建：指令 + 覆盖摘要（修复前的做法） | `b28014bcf978953cd8914ab73194282b9f1aa699e5b1210d19e93af640c0bed8`（不一致） |

## 复现命令（概要）

```sh
cp -a <poa-f2-c>/tmp/pg-f2c-live <scratch>/f2clive && rm -f <scratch>/f2clive/postmaster.pid
pg_ctl -D <scratch>/f2clive -o "-p 55724 -k '' -c listen_addresses=127.0.0.1" -w start
git worktree add --detach <scratch>/main-a7b296a a7b296a
PYTHONPATH=<scratch>/main-a7b296a .venv/bin/python <scratch>/rebuild_1b21375d.py   # INCOMPATIBLE_STATE
PYTHONPATH=<poa-304>              .venv/bin/python <scratch>/rebuild_1b21375d.py   # ok + 哈希一致
pg_ctl -D <scratch>/f2clive -m fast stop
```

脚本先只读加载该 Run 的 run 行、步骤行和 inputs，再拼成与 `DurableStore.rebuild` 同形的 snapshot（固定为这个 Run，不按 incident 的 current_run 取），然后调用 `rebuild_transcript`。哈希比对时，先分别重建到 round-7 和 round-8，取得当时的 delivered views 和 transcript，再按 `_final_messages` 的顺序拼出消息并取哈希。脚本没有入库，因为它只针对这一份一次性的数据副本。

## 不在本证据范围

- 不是 LangSmith trace，也不是一次新的真实 Run；D-U2 ②（kind 最小 Run）另行记录。
- 1b21375d 仍是 `cancelled`，没有复活；F2 证据中断言 2 记为 FAIL 的结论不改写。
